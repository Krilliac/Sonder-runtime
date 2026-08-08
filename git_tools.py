"""Bounded, read-only Git inspection for guarded Sonder workspaces.

Only fixed Git subcommands assembled by this module are executed.  Caller text
is never interpreted by a shell, repository hooks are not invoked, optional
index locks are disabled, and output is drained through a shared byte budget so
a large diff cannot grow the runtime process without bound.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import signal
import subprocess
import threading
import time

import file_ops
import sonder_logging


DEFAULT_TIMEOUT = 10
MAX_TIMEOUT = 30
DEFAULT_OUTPUT_BYTES = 128_000
MAX_OUTPUT_BYTES = 256_000
MAX_DIFF_CONTEXT = 20


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
    env = sonder_logging.child_environment()
    env.update({
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
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
    })
    return parsed


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
    resolved = file_ops.resolve_repository_read_path(
        str(candidate),
        allow_workspace_root=False,
        reject_sensitive=True,
        extra_roots=authorized_roots,
    )
    try:
        relative = resolved.relative_to(root)
    except ValueError as exc:
        raise PermissionError("diff path is outside the repository root") from exc
    return relative.as_posix()


def repo_diff(root=".", *, staged=False, path="", context=3,
              timeout=DEFAULT_TIMEOUT, max_output=DEFAULT_OUTPUT_BYTES,
              extra_roots="", bypass=False):
    """Return a bounded working-tree or staged diff with optional path scope."""
    root = _resolve_root(root, extra_roots=extra_roots, bypass=bypass)
    top = _require_repository_root(root, timeout=timeout, max_output=max_output)
    relative = _resolve_diff_path(top, path, extra_roots=extra_roots)
    context = _bounded_int(context, 3, 0, MAX_DIFF_CONTEXT)
    arguments = [
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
