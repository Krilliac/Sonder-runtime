"""Bounded, read-only Git inspection for guarded Sonder workspaces.

Only fixed Git subcommands assembled by this module are executed.  Caller text
is never interpreted by a shell, repository hooks are not invoked, optional
index locks are disabled, and output is drained through a shared byte budget so
a large diff cannot grow the runtime process without bound.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import threading
import time
from datetime import datetime, timezone

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_logging


DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 30
DEFAULT_OUTPUT_BYTES = 128_000
MAX_OUTPUT_BYTES = 256_000
MAX_DIFF_CONTEXT = 20

# Updating the running source tree is deliberately narrower than ordinary Git
# project tools.  It is a local-developer convenience for this repository, not
# a general "pull whatever remote the model names" capability.
RUNTIME_UPDATE_REMOTE = "origin"
RUNTIME_UPDATE_BRANCH = "main"
RUNTIME_STASH_MESSAGE = "sonder runtime recovery"
_TRUSTED_RUNTIME_ORIGINS = frozenset({
    "https://github.com/krilliac/sonder-runtime",
    "git@github.com:krilliac/sonder-runtime",
    "ssh://git@github.com/krilliac/sonder-runtime",
})


def _bounded_int(value, default, minimum, maximum):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _resolve_root(root, *, extra_roots="", bypass=False):
    resolved = file_ops.resolve_path(
        str(root or "."), extra_roots=extra_roots, bypass=bypass,
    )
    if not resolved.is_dir():
        raise ValueError("repository root is not a directory: %s" % resolved)
    return resolved


def _git_environment():
    # Git's ambient environment can redirect the repository/config/index or
    # inject command-line config entries. Start from a clean Git namespace and
    # add back only explicit noninteractive safety controls.
    env = {
        key: value for key, value in sonder_logging.child_environment().items()
        if not key.upper().startswith("GIT_") and key.upper() != "SSH_ASKPASS"
    }
    env.update({
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
        # SSH treats a closed stdin as a reason to fall back to an askpass
        # program when DISPLAY and SSH_ASKPASS are inherited.  Runtime update
        # checks must fail instead of executing that ambient helper or opening
        # a credential-manager prompt.  This still permits noninteractive
        # public-key authentication for the canonical GitHub remote.
        "SSH_ASKPASS_REQUIRE": "never",
        "GCM_INTERACTIVE": "never",
    })
    return env


def _drain_bounded(pipe, sink, budget, lock):
    try:
        while True:
            chunk = pipe.read(16_384)
            if not chunk:
                return
            with lock:
                budget["seen"] += len(chunk)
                take = min(len(chunk), budget["remaining"])
                if take:
                    sink.extend(chunk[:take])
                    budget["remaining"] -= take
    finally:
        try:
            pipe.close()
        except OSError:
            pass


def _terminate(proc):
    if proc.poll() is not None:
        return
    try:
        if os.name == "nt":
            proc.kill()
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def _run_git(root, arguments, *, timeout=DEFAULT_TIMEOUT,
             max_output=DEFAULT_OUTPUT_BYTES):
    """Run one fixed Git argv and return bounded decoded process evidence."""
    executable = shutil.which("git")
    if not executable:
        raise FileNotFoundError("git was not found on PATH")
    timeout = _bounded_int(timeout, DEFAULT_TIMEOUT, 1, MAX_TIMEOUT)
    max_output = _bounded_int(
        max_output, DEFAULT_OUTPUT_BYTES, 1, MAX_OUTPUT_BYTES,
    )
    command = [executable, "-C", str(root), *list(arguments)]
    creationflags = 0
    if os.name == "nt":
        creationflags = (
            getattr(subprocess, "CREATE_NO_WINDOW", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    started = time.monotonic()
    proc = subprocess.Popen(
        command,
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        creationflags=creationflags,
        start_new_session=os.name != "nt",
        env=_git_environment(),
    )
    budget = {"remaining": max_output, "seen": 0}
    lock = threading.Lock()
    stdout = bytearray()
    stderr = bytearray()
    readers = [
        threading.Thread(
            target=_drain_bounded,
            args=(proc.stdout, stdout, budget, lock),
            daemon=True,
        ),
        threading.Thread(
            target=_drain_bounded,
            args=(proc.stderr, stderr, budget, lock),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _terminate(proc)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            _terminate(proc)
    for reader in readers:
        reader.join(timeout=2)
    return {
        "command": command,
        "returncode": proc.returncode,
        "timed_out": timed_out,
        "elapsed_ms": int((time.monotonic() - started) * 1000),
        "stdout": bytes(stdout).decode("utf-8", errors="replace"),
        "stderr": bytes(stderr).decode("utf-8", errors="replace"),
        "output_bytes": budget["seen"],
        "output_limit": max_output,
        "truncated": budget["seen"] > max_output,
    }


def _checked_git(root, arguments, *, timeout, max_output, operation):
    result = _run_git(
        root, arguments, timeout=timeout, max_output=max_output,
    )
    if result["timed_out"]:
        raise TimeoutError("git %s timed out after %s second(s)" % (
            operation, _bounded_int(timeout, DEFAULT_TIMEOUT, 1, MAX_TIMEOUT),
        ))
    if result["returncode"] != 0:
        detail = (result["stderr"] or result["stdout"]).strip()
        if result["truncated"]:
            detail += " [output truncated]"
        raise ValueError(
            "git %s failed (exit %s): %s" % (
                operation, result["returncode"], detail or "no diagnostic",
            )
        )
    return result


def _require_repository_root(root, *, timeout, max_output):
    result = _checked_git(
        root,
        ["rev-parse", "--path-format=absolute", "--show-toplevel"],
        timeout=timeout,
        max_output=min(max_output, 16_384),
        operation="root probe",
    )
    top_text = result["stdout"].strip()
    if not top_text:
        raise ValueError("git root probe returned no repository root")
    top = Path(top_text).resolve()
    if os.path.normcase(str(top)) != os.path.normcase(str(root.resolve())):
        raise PermissionError(
            "repository root must name the Git top-level; refusing upward "
            "discovery from %s to %s" % (root, top)
        )
    return top


def _parse_status(text):
    branch = ""
    oid = ""
    upstream = ""
    ahead = 0
    behind = 0
    entries = []
    for line in text.splitlines():
        if line.startswith("# branch.head "):
            branch = line[len("# branch.head "):].strip()
        elif line.startswith("# branch.oid "):
            oid = line[len("# branch.oid "):].strip()
        elif line.startswith("# branch.upstream "):
            upstream = line[len("# branch.upstream "):].strip()
        elif line.startswith("# branch.ab "):
            fields = line.split()
            try:
                ahead = int(fields[-2].lstrip("+"))
                behind = abs(int(fields[-1]))
            except (IndexError, ValueError):
                pass
        elif line and not line.startswith("# "):
            entries.append(line)
    detached = branch == "(detached)"
    return {
        "branch": "" if detached else branch,
        "detached": detached,
        "oid": oid,
        "upstream": upstream,
        "ahead": ahead,
        "behind": behind,
        "clean": not entries,
        "change_count": len(entries),
        "entries": entries,
    }


def repo_status(root=".", *, timeout=DEFAULT_TIMEOUT,
                max_output=DEFAULT_OUTPUT_BYTES, extra_roots="", bypass=False):
    """Return bounded porcelain-v2 branch and worktree status evidence."""
    root = _resolve_root(root, extra_roots=extra_roots, bypass=bypass)
    top = _require_repository_root(root, timeout=timeout, max_output=max_output)
    result = _checked_git(
        top,
        [
            "-c", "color.ui=false", "--no-pager", "status",
            "--porcelain=v2", "--branch", "--untracked-files=all",
        ],
        timeout=timeout,
        max_output=max_output,
        operation="status",
    )
    parsed = _parse_status(result["stdout"])
    parsed.update({
        "root": str(top),
        "elapsed_ms": result["elapsed_ms"],
        "truncated": result["truncated"],
        "output_bytes": result["output_bytes"],
        "output_limit": result["output_limit"],
        "complete": not result["truncated"],
    })
    if result["truncated"]:
        # Absence of a change record in a truncated prefix proves nothing.
        parsed["clean"] = None
    return parsed


def _runtime_git_text(root, arguments, *, operation, timeout=DEFAULT_TIMEOUT):
    return _checked_git(
        root, arguments, timeout=timeout, max_output=16_384, operation=operation,
    )["stdout"].strip()


def _runtime_remote_url(root):
    return _runtime_git_text(
        root, ["remote", "get-url", RUNTIME_UPDATE_REMOTE],
        operation="remote URL probe",
    )


def runtime_checkout_commit(root, *, timeout=DEFAULT_TIMEOUT):
    """Return the commit loaded from a fixed runtime checkout.

    This is intentionally narrower than :func:`runtime_update_status`: it
    neither inspects a remote ref nor fetches.  The server snapshots it once
    during import so status can distinguish the bytes Python loaded from a
    later on-disk fast-forward that still requires a restart.
    """
    root = Path(root).resolve()
    top = _require_repository_root(root, timeout=timeout, max_output=16_384)
    return _runtime_git_text(top, ["rev-parse", "HEAD"], operation="runtime startup HEAD probe")


def _normalise_remote_url(value):
    return str(value or "").strip().rstrip("/").removesuffix(".git").casefold()


def _trusted_runtime_origin(value):
    return _normalise_remote_url(value) in _TRUSTED_RUNTIME_ORIGINS


def _runtime_fetch_arguments():
    """Return the fixed, configuration-neutral update fetch command.

    The canonical remote check prevents a substituted origin, but Git still
    reads repository configuration while it fetches.  Do not let a writable
    checkout turn a routine update check into credential-helper/askpass code
    execution, nor let a configured remote refspec create additional refs.
    The explicit refspec below is the sole ref update this operation permits.
    """
    return [
        "-c", "remote.%s.uploadpack=" % RUNTIME_UPDATE_REMOTE,
        "-c", "core.sshCommand=",
        "-c", "credential.helper=",
        "-c", "core.askPass=",
        "fetch", "--no-tags", "--prune", "--refmap=", RUNTIME_UPDATE_REMOTE,
        "+refs/heads/%s:refs/remotes/%s/%s" % (
            RUNTIME_UPDATE_BRANCH, RUNTIME_UPDATE_REMOTE, RUNTIME_UPDATE_BRANCH,
        ),
    ]


def runtime_update_status(root, *, refresh=False, timeout=DEFAULT_TIMEOUT):
    """Return bounded source-tree update evidence for Sonder itself.

    Unlike :func:`repo_status`, this accepts only an already-selected runtime
    source root supplied by the host.  ``refresh`` fetches only ``origin/main``
    and never changes the worktree.
    """
    root = Path(root).resolve()
    top = _require_repository_root(root, timeout=timeout, max_output=16_384)
    remote_url = _runtime_remote_url(top)
    if refresh and not _trusted_runtime_origin(remote_url):
        raise PermissionError("runtime update check requires the canonical Sonder origin remote")
    if refresh:
        _checked_git(
            top,
            _runtime_fetch_arguments(),
            timeout=timeout, max_output=16_384, operation="update fetch",
        )
    local = _runtime_git_text(top, ["rev-parse", "HEAD"], operation="HEAD probe")
    remote_ref = "%s/%s" % (RUNTIME_UPDATE_REMOTE, RUNTIME_UPDATE_BRANCH)
    newest_commit = _runtime_git_text(
        top, ["rev-parse", "--verify", remote_ref], operation="remote HEAD probe",
    )
    counts = _runtime_git_text(
        top, ["rev-list", "--left-right", "--count", "HEAD...%s" % remote_ref],
        operation="update distance",
    ).split()
    if len(counts) != 2:
        raise ValueError("git update distance returned malformed output")
    try:
        ahead, behind = (int(counts[0]), int(counts[1]))
    except ValueError as exc:
        raise ValueError("git update distance returned non-numeric output") from exc
    status = repo_status(top, timeout=timeout, max_output=16_384, bypass=True)
    branch = status.get("branch") or ""
    if ahead and behind:
        state = "diverged"
    elif ahead:
        state = "ahead"
    elif behind:
        state = "behind"
    else:
        state = "current"
    return {
        "root": str(top),
        "branch": branch,
        "installed_commit": local,
        "installed_commit_time": _runtime_git_text(
            top, ["show", "-s", "--format=%cI", "HEAD"], operation="installed commit time",
        ),
        "newest_commit": newest_commit,
        "newest_commit_time": _runtime_git_text(
            top, ["show", "-s", "--format=%cI", remote_ref], operation="remote commit time",
        ),
        "ahead": ahead,
        "behind": behind,
        "state": state,
        "clean": status.get("clean") is True,
        "remote": remote_url,
        "trusted_remote": _trusted_runtime_origin(remote_url),
        # A cached remote-tracking ref is useful at REPL startup, but it is not
        # proof that the network's current origin/main has been observed. Keep
        # that distinction in structured data so renderers never present a
        # stale cache as a freshly checked update verdict.
        "remote_ref_refreshed": bool(refresh),
        "checked_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def runtime_update(root, *, timeout=MAX_TIMEOUT):
    """Fast-forward this verified clean source checkout to ``origin/main``.

    Hooks are disabled and no merge/rebase/conflict resolution is attempted.
    Callers must restart the running process after a successful update.
    """
    root = Path(root).resolve()
    top = _require_repository_root(root, timeout=timeout, max_output=16_384)
    if not _trusted_runtime_origin(_runtime_remote_url(top)):
        raise PermissionError("runtime update requires the canonical Sonder origin remote")
    before = runtime_update_status(top, refresh=True, timeout=timeout)
    if before["branch"] != RUNTIME_UPDATE_BRANCH:
        raise PermissionError(
            "runtime update requires branch %r (current checkout: %r); "
            "switch the clean canonical checkout to %r, then retry"
            % (RUNTIME_UPDATE_BRANCH, before["branch"] or "detached HEAD", RUNTIME_UPDATE_BRANCH)
        )
    if not before["clean"]:
        raise PermissionError("runtime update refuses a dirty source checkout")
    if before["ahead"]:
        raise PermissionError("runtime update refuses local commits; reconcile them manually")
    if before["state"] == "current":
        return {"updated": False, "before": before, "after": before}
    hooks_path = str(Path(before["root"]) / ".sonder-disabled-git-hooks")
    _checked_git(
        Path(before["root"]),
        [
         *_neutralized_filter_arguments(
             Path(before["root"]), timeout=timeout, max_output=65_536,
         ),
         "-c", "core.hooksPath=" + hooks_path,
         "-c", "core.sshCommand=",
         "merge", "--ff-only", "--no-edit", "--no-overwrite-ignore",
         "%s/%s" % (RUNTIME_UPDATE_REMOTE, RUNTIME_UPDATE_BRANCH)],
        timeout=timeout, max_output=64_000, operation="fast-forward update",
    )
    after = runtime_update_status(root, refresh=False, timeout=timeout)
    return {"updated": True, "before": before, "after": after}


def _require_runtime_checkout(root, *, timeout):
    """Resolve the one checkout runtime-maintenance may ever modify."""
    top = _require_repository_root(Path(root).resolve(), timeout=timeout, max_output=16_384)
    if not _trusted_runtime_origin(_runtime_remote_url(top)):
        raise PermissionError("runtime recovery requires the canonical Sonder origin remote")
    branch = _runtime_git_text(top, ["branch", "--show-current"], operation="branch probe")
    if branch != RUNTIME_UPDATE_BRANCH:
        raise PermissionError(
            "runtime recovery requires branch %r (current checkout: %r)"
            % (RUNTIME_UPDATE_BRANCH, branch or "detached HEAD")
        )
    return top


def _runtime_mutation_arguments(root, subcommand):
    """Keep checkout filters and hooks inert for runtime recovery actions."""
    hooks_path = str(Path(root) / ".sonder-disabled-git-hooks")
    return [
        *_neutralized_filter_arguments(Path(root), timeout=MAX_TIMEOUT, max_output=65_536),
        "-c", "core.hooksPath=" + hooks_path,
        "-c", "core.sshCommand=",
        *subcommand,
    ]


def runtime_stash_status(root, *, timeout=DEFAULT_TIMEOUT):
    """Return bounded, content-free recovery readiness and stash metadata.

    Only stashes bearing the host-created recovery marker are exposed here.
    A caller must never mistake an unrelated developer stash for a Sonder
    recovery checkpoint, particularly because ``pop`` both restores and drops
    its selected stash.
    """
    top = _require_runtime_checkout(root, timeout=timeout)
    status = repo_status(top, timeout=timeout, max_output=16_384, bypass=True)
    listed = _checked_git(
        top,
        ["stash", "list", "--format=%gd%x00%gs"],
        timeout=timeout, max_output=16_384, operation="stash list",
    )
    entries = []
    for line in listed["stdout"].splitlines():
        reference, separator, subject = line.strip().partition("\x00")
        if not separator or not reference or RUNTIME_STASH_MESSAGE not in subject:
            continue
        entries.append(reference)
    return {
        "root": str(top),
        "branch": status.get("branch") or "",
        "clean": status.get("clean") is True,
        "change_count": int(status.get("change_count") or 0),
        "stash_count": len(entries),
        "top": entries[0] if entries else "",
    }


def runtime_stash(root, action, *, timeout=MAX_TIMEOUT):
    """Save or restore the most recent source-recovery stash for canonical main.

    This is purposefully not a general Git stash wrapper: actions, message,
    checkout, and stash selector are all fixed by the host.  ``pop`` requires
    an empty checkout to avoid merging an old recovery into fresh local edits.
    """
    selected = str(action or "").strip().lower().replace("_", "-")
    if selected not in {"save", "save-untracked", "pop"}:
        raise ValueError("runtime stash action must be save, save-untracked, or pop")
    top = _require_runtime_checkout(root, timeout=timeout)
    before = runtime_stash_status(top, timeout=timeout)
    if selected.startswith("save"):
        if before["clean"]:
            return {"action": selected, "changed": False, "before": before, "after": before}
        command = ["stash", "push", "--message", RUNTIME_STASH_MESSAGE]
        if selected == "save-untracked":
            command.append("--include-untracked")
        _checked_git(
            top,
            _runtime_mutation_arguments(top, command),
            timeout=timeout, max_output=64_000, operation="stash save",
        )
    else:
        if not before["clean"]:
            raise PermissionError("runtime stash pop requires a clean source checkout")
        if not before["stash_count"]:
            raise ValueError("runtime stash pop requires an existing recovery stash")
        _checked_git(
            top,
            _runtime_mutation_arguments(top, ["stash", "pop", before["top"]]),
            timeout=timeout, max_output=64_000, operation="stash pop",
        )
    return {
        "action": selected,
        "changed": True,
        "before": before,
        "after": runtime_stash_status(top, timeout=timeout),
    }


def _resolve_diff_path(root, path, *, extra_roots):
    raw = str(path or "").strip()
    if not raw:
        return ""
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    authorized_roots = str(root)
    if extra_roots:
        authorized_roots += os.pathsep + str(extra_roots)
    # Validate the parent physically, but preserve the final lexical component.
    # A tracked symlink is a Git entry in its own right: resolving its target
    # changes the pathspec and can incorrectly reject a safe link to outside.
    resolved_parent = file_ops.resolve_repository_read_path(
        str(candidate.parent),
        allow_workspace_root=True,
        reject_sensitive=True,
        extra_roots=authorized_roots,
    )
    try:
        parent_relative = resolved_parent.relative_to(root)
    except ValueError as exc:
        raise PermissionError("diff path is outside the repository root") from exc
    basename = candidate.name
    if not basename or basename in {".", ".."}:
        raise ValueError("diff path must name a repository entry")
    lowered = basename.casefold()
    if (
        file_ops._is_secret_path(Path(basename))
        or lowered in {str(name).casefold() for name in file_ops.CONTROL_CONFIG_FILES}
        or lowered in {"memory.db", "memory.db-wal", "memory.db-shm"}
    ):
        raise PermissionError("diff path is secret or control state")
    relative = parent_relative / basename
    return relative.as_posix()


_FILTER_COMMAND_RE = re.compile(r"^(filter\..*)\.(?:clean|smudge|process)$", re.IGNORECASE)


def _neutralized_filter_arguments(root, *, timeout, max_output):
    """Return command-line overrides for every configured checkout filter."""
    result = _run_git(
        root,
        ["config", "--null", "--name-only", "--get-regexp", r"^filter\..*\.(clean|smudge|process)$"],
        timeout=timeout, max_output=min(max_output, 65_536),
    )
    if result["timed_out"]:
        raise TimeoutError("git filter configuration probe timed out")
    if result["returncode"] not in {0, 1}:
        detail = (result["stderr"] or result["stdout"]).strip()
        raise ValueError("git filter configuration probe failed: %s" % (detail or "no diagnostic"))
    if result["truncated"]:
        raise PermissionError("git filter configuration is too large to neutralize safely")
    drivers = set()
    for key in result["stdout"].split("\x00"):
        key = key.strip()
        if not key:
            continue
        match = _FILTER_COMMAND_RE.fullmatch(key)
        if not match:
            raise PermissionError("unexpected Git filter configuration key: %s" % key)
        drivers.add(match.group(1))
    overrides = []
    for driver in sorted(drivers, key=lambda value: (value.casefold(), value)):
        # `process=` disables the long-running protocol; fixed passthrough
        # commands replace repository-controlled clean/smudge programs.
        overrides.extend([
            "-c", "%s.process=" % driver,
            "-c", "%s.clean=cat" % driver,
            "-c", "%s.smudge=cat" % driver,
            "-c", "%s.required=false" % driver,
        ])
    return overrides


def repo_diff(root=".", *, staged=False, path="", context=3,
              timeout=DEFAULT_TIMEOUT, max_output=DEFAULT_OUTPUT_BYTES,
              extra_roots="", bypass=False):
    """Return a bounded working-tree or staged diff with optional path scope."""
    root = _resolve_root(root, extra_roots=extra_roots, bypass=bypass)
    top = _require_repository_root(root, timeout=timeout, max_output=max_output)
    relative = _resolve_diff_path(top, path, extra_roots=extra_roots)
    context = _bounded_int(context, 3, 0, MAX_DIFF_CONTEXT)
    arguments = [
        *_neutralized_filter_arguments(
            top, timeout=timeout, max_output=max_output,
        ),
        "-c", "color.ui=false", "-c", "core.pager=cat",
        "--no-pager", "--literal-pathspecs", "diff", "--no-ext-diff",
        "--no-textconv",
        "--unified=%d" % context,
    ]
    if staged is True:
        arguments.append("--cached")
    arguments.append("--")
    if relative:
        arguments.append(relative)
    result = _checked_git(
        top, arguments, timeout=timeout, max_output=max_output,
        operation="diff",
    )
    return {
        "root": str(top),
        "staged": staged is True,
        "path": relative,
        "context": context,
        "diff": result["stdout"],
        "elapsed_ms": result["elapsed_ms"],
        "truncated": result["truncated"],
        "output_bytes": result["output_bytes"],
        "output_limit": result["output_limit"],
    }
