"""Sonder developer-workflow tools.

Provides test running, linting, formatting, type checking, dependency
management, git mutations, refactoring helpers, build tools, and security
scanning for Sonder's MCP tool surface.
"""
from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
from sonder_runtime.domain.agents import interpreter_argv as _interpreter_argv
from sonder_runtime.application.testing.legacy_runs import retired_extra_args
from sonder_runtime.adapters import git_change_targets
from sonder_runtime.adapters import live_checkout
from sonder_runtime.adapters import git_program_guard
from sonder_runtime.adapters import process_tree
from sonder_runtime.adapters import confined_read
from sonder_runtime.adapters import formatter_config_guard
from sonder_runtime.adapters import project_venv
from sonder_runtime.adapters import protected_write_scope
from sonder_runtime.adapters import requirements_guard
from sonder_runtime.adapters import secret_scan as _secret_scan_adapter
from sonder_runtime.adapters.git_mutation_guard import (
    ConcurrentGitMutation,
    guard_git_mutation,
)
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread
import git_tools
import sonder_logging
import unsafe_lab

# Strict PEP 508 parsing for ``requirements_guard`` (the adapters layer may not import a
# third-party package, so it is injected from here). ``packaging`` is not a declared runtime
# dependency; pip, which every pip lane already needs, vendors the same module.
try:
    from packaging.requirements import Requirement as _PEP508Requirement
except ImportError:  # pragma: no cover - depends on the interpreter's installed packages
    try:
        from pip._vendor.packaging.requirements import Requirement as _PEP508Requirement
    except ImportError:
        _PEP508Requirement = None

MAX_OUTPUT = 256_000
MAX_TIMEOUT = 120
DEFAULT_TIMEOUT = 30
# The ``guard`` of a ``git_stash`` refusal that would take the runtime recovery stash.
RECOVERY_STASH_GUARD = "runtime_recovery_stash"


def _bounded_int(value, default, minimum, maximum):
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(v, maximum))


def _trim(text, limit=MAX_OUTPUT):
    text = str(text or "")
    if len(text) <= limit:
        return text
    half = limit // 2
    return text[:half] + "\n... [trimmed %d chars] ...\n" % (len(text) - limit) + text[-half:]


def _resolve_root(root, extra_roots=""):
    """Resolve a caller-supplied working root INSIDE an authorized root.

    Every tool in this module hands ``root`` straight to a child process as its
    working directory, and several of them report what they find there --
    ``secret_scan`` reports which files hold credentials (it once printed
    the credentials themselves; its findings are now redacted). Until now this function
    resolved any absolute path and returned it, so the only thing standing
    between a caller and an arbitrary directory was whether a caller could
    reach these functions at all. ``fix/cloud-help-drift`` @ ``b8a15ef`` relied
    on exactly that, removing the four read-only tools from the agent surface
    on the stated grounds that "adding a dispatch branch would have handed a
    read-only agent unconfined filesystem read"; a later lane added the dispatch
    branches back, and the unreachability that was doing the work went away.

    Reachability is not a control. Confinement is, so it lives here -- one layer
    below every entry point, which also covers the direct MCP tools that were
    never confined either. The authorized set is ``file_ops.allowed_roots()``:
    Sonder's workspace, its home, ``SONDER_FILE_ROOTS`` and ``file_roots.local``
    -- exactly what the guarded file tools already honor, so this grants nothing
    the operator has not already granted them. ``extra_roots`` carries the
    host-selected project root and reaches here only through a host-controlled
    parameter, never from model-supplied arguments.

    Authorization is checked BEFORE the directory stat, and the unauthorized
    refusal names no path. The other order answered "does this exist?" before
    "are you allowed to ask?", and the two refusals were distinguishable::

        unauthorized MISSING  -> ValueError: not a directory: <unapproved-root>
        unauthorized EXISTING -> PermissionError: root is outside every authorized root: <unapproved-root>

    Every server wrapper does ``return "ERROR: %s" % exc``, so a confined agent
    could probe the existence of any path on the host and read the resolved
    location back -- the same class of leak the ``diff_files`` absolute-path
    fix closed in this module. An authorized root may still report "not a
    directory" with its path: the caller is entitled to that one.
    """
    raw = str(root or ".")
    raw = file_ops._reject_network_path(raw)
    if raw.strip().replace("/", "\\") == "\\??":
        raise PermissionError("refusing a UNC or network path spelling")
    p = Path(raw).resolve()
    _require_authorized_root(p, extra_roots)
    if not p.is_dir():
        raise ValueError("not a directory: %s" % p)
    return p


def _resolve_target_path(root, path):
    """Confine the SECOND argument -- the one appended to the child's argv.

    ``_resolve_root`` confines the working directory. It was the only control,
    and four tools take a second argument that is not a working directory:
    ``test_run``, ``lint_run``, ``format_code`` and ``typecheck_run`` do
    ``cmd.append(path)`` and the child resolves it against ``cwd=root``. So
    ``path`` was checked by nothing.

    ``server.py`` already wrote this fact down at its own layer --
    *"Containing `root` alone leaves `path` checked by nothing, so
    lint_run(path='../../x', fix=True) would write outside the project"* -- and
    closed it only on the project-bound agent path, leaving the direct
    ``@mcp.tool()`` callers and every unbound run open. Measured, the exposure
    is not limited to writes: with pytest as the framework,
    ``test_run(root=<authorized>, path="../OUTSIDE/test_evil.py")`` returned
    ``ok=True, "1 passed"`` having **executed** a file outside the authorized
    root, which wrote a marker there.

    Confinement therefore belongs here, one layer below all four entry points,
    for the same reason ``_resolve_root``'s docstring gives: reachability is
    not a control.

    Returns the path to hand the child -- relative to ``root`` so the argv keeps
    the same shape (and does not leak the absolute host path, as
    ``diff_files`` was fixed not to). An empty ``path`` stays empty; callers
    already branch on that.
    """
    text = str(path or "").strip()
    if not text:
        return ""
    if any(ch in text for ch in ("\0", "\r", "\n")):
        raise ValueError("invalid path control character: %r" % text)
    if text.startswith("-"):
        raise ValueError("path must not start with '-': %r" % text)
    text = file_ops._reject_network_path(text)
    root = Path(root)
    candidate = Path(text)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and not file_ops._is_inside(resolved, root):
        raise ValueError(
            "path is outside the root it was given: %s is not inside %s"
            % (text, root)
        )
    try:
        rel = str(resolved.relative_to(root)) or "."
    except ValueError:
        rel = str(resolved)
    if rel.startswith("-"):
        raise ValueError("path must not start with '-': %r" % text)
    return rel


_AUTHORIZED_ROOT = contextvars.ContextVar("harness_authorized_root", default="")


@contextlib.contextmanager
def authorized_root_scope(root):
    """Authorize ONE host-selected root for the duration of a dispatch.

    This is a scope rather than an argument on purpose. A model can put any
    string in a tool's arguments, so an ``extra_roots`` argument on the agent
    surface would be a root it grants itself -- the same forgery
    ``_TRUSTED_REPOSITORY_APPROVAL`` exists to prevent for the guarded file
    tools. Only ``server._agent_dispatch`` opens this scope, and only with the
    root the host chose. Every other caller sees the default of "" and is
    confined to the operator's configured roots.
    """
    token = _AUTHORIZED_ROOT.set(str(root or ""))
    try:
        yield
    finally:
        _AUTHORIZED_ROOT.reset(token)


def _require_authorized_root(resolved, extra_roots=""):
    if unsafe_lab.active() or file_ops.bypass_enabled():
        # The deliberately unrestricted process, and the operator's explicit
        # env bypass, already remove every other file-policy control.
        return
    extra_roots = extra_roots or _AUTHORIZED_ROOT.get()
    for root in file_ops.allowed_roots(extra_roots):
        try:
            candidate = root.resolve()
        except OSError:
            candidate = root
        if resolved == candidate or file_ops._is_inside(resolved, candidate):
            return
    # Deliberately names NO path. The resolved path is the host's real
    # location -- account name included -- and every server wrapper returns
    # `"ERROR: %s" % exc` straight to a confined agent, so echoing it here
    # disclosed both that the path exists and where it actually lives.
    # `_resolve_root` explains the ordering half of the same leak.
    raise PermissionError(
        "root is outside every authorized root. Add it to file_roots.local "
        "or SONDER_FILE_ROOTS, or pass a project the host has selected."
    )


def _child_env():
    # Developer workflow commands execute repository-controlled scripts (test
    # hooks, package managers, formatters, and build tools).  They must not
    # inherit the runtime's API keys or approval/bypass controls: those scripts
    # can print or encode their environment into the tool result.  Keep this
    # in the shared helper used by the other model-directed execution lanes so
    # future additions cannot silently diverge from the control-plane policy.
    env = sonder_logging.child_environment()
    env.pop("CC", None)
    env.pop("CXX", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _drain_pipe(pipe, sink, state, limit):
    """Drain *pipe* continuously while bounding retained RAM to *limit* bytes."""
    head_limit = limit // 2
    try:
        while True:
            block = pipe.read(65536)
            if not block:
                break
            state["bytes"] += len(block)
            sink.extend(block)
            if len(sink) > limit:
                del sink[head_limit:len(sink) - (limit - head_limit)]
    finally:
        with contextlib.suppress(OSError):
            pipe.close()


def _write_stdin(pipe, payload):
    try:
        if payload:
            pipe.write(payload)
            pipe.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        with contextlib.suppress(OSError):
            pipe.close()


def _format_captured_stream(sink, total_bytes, limit=MAX_OUTPUT):
    if total_bytes <= limit:
        return _trim(bytes(sink).decode("utf-8", errors="replace"), limit=limit)
    head_limit = limit // 2
    head = bytes(sink[:head_limit]).decode("utf-8", errors="replace")
    tail = bytes(sink[head_limit:]).decode("utf-8", errors="replace")
    return head + "\n... [trimmed %d bytes] ...\n" % (total_bytes - limit) + tail


# Characters cmd.exe interprets in a batch file's command line. CreateProcess
# runs a .cmd/.bat through cmd.exe, which re-parses arguments ("BatBadBut");
# subprocess quoting does not escape for those, so an unsafe character could run
# a second command.
_BATCH_UNSAFE_ARGUMENT = re.compile(r'["%!^&|<>()\r\n]')


def _windows_executable(cmd):
    """``cmd`` with a bare or path-prefixed program resolved via PATH and PATHEXT.

    CreateProcess appends only ``.exe``, so ``npx``/``npm``/``pnpm``/``yarn``
    (shipped as ``.cmd``) were not found without ``shutil.which``. Raises
    ``ValueError`` when ``cmd`` is empty or when the target is a ``.cmd``/``.bat``
    script and any argument contains a character ``cmd.exe`` would interpret.
    """
    if not cmd:
        raise ValueError("command must not be empty")
    first = str(cmd[0])
    found = shutil.which(first)
    if found and not os.path.isabs(found):
        # Python's lookup searches the current folder first on Windows and CreateProcess
        # resolves a relative program against the parent's folder, so a project file wins.
        raise ValueError(
            "refusing to run %s: the lookup gave the relative path %r (the current folder or a "
            "relative PATH entry)" % (first, found))
    found = found or first
    if found.rstrip(" .").lower().endswith((".cmd", ".bat")):
        for argument in cmd[1:]:
            if _BATCH_UNSAFE_ARGUMENT.search(str(argument)):
                raise ValueError(
                    "refusing to run %s: argument %r holds a character cmd.exe would "
                    "interpret in a batch file" % (first, str(argument))
                )
    return [found, *cmd[1:]]


def _run(cmd, *, cwd, timeout=DEFAULT_TIMEOUT, stdin_bytes=b"", env=None):
    started = time.time()
    if os.name == "nt":
        try:
            launch = _windows_executable(cmd)
        except ValueError as exc:
            return {
                "ok": False, "returncode": -1, "timed_out": False,
                "elapsed_ms": int((time.time() - started) * 1000),
                "stdout": "", "stderr": str(exc),
                "command": [str(c) for c in cmd],
            }
    else:
        launch = list(cmd)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            launch,
            cwd=str(cwd),
            stdin=subprocess.PIPE if stdin_bytes else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=env if env is not None else _child_env(),
            **process_tree.group_popen_kwargs(creationflags),
        )
    except FileNotFoundError:
        return {
            "ok": False, "returncode": -1, "timed_out": False,
            "elapsed_ms": int((time.time() - started) * 1000),
            "stdout": "", "stderr": "command not found: %s" % cmd[0],
            "command": [str(c) for c in cmd],
        }
    except OSError as exc:
        # e.g. WinError 216 for a .com file that is really batch text or a foreign binary.
        return {
            "ok": False, "returncode": -1, "timed_out": False,
            "elapsed_ms": int((time.time() - started) * 1000),
            "stdout": "", "stderr": "could not start %s: %s" % (cmd[0], exc),
            "command": [str(c) for c in cmd],
        }
    tree = (
        process_tree.ProcessTree.owning(proc)
        if process_tree.is_real_popen(proc) else None
    )
    stdout_bytes = bytearray()
    stderr_bytes = bytearray()
    stdout_state = {"bytes": 0}
    stderr_state = {"bytes": 0}
    threads = []
    timed_out = False
    try:
        readers = [
            owned_runtime_thread(
                target=_drain_pipe,
                args=(proc.stdout, stdout_bytes, stdout_state, MAX_OUTPUT),
                daemon=True,
            ),
            owned_runtime_thread(
                target=_drain_pipe,
                args=(proc.stderr, stderr_bytes, stderr_state, MAX_OUTPUT),
                daemon=True,
            ),
        ]
        if stdin_bytes and proc.stdin is not None:
            readers.append(
                owned_runtime_thread(
                    target=_write_stdin,
                    args=(proc.stdin, stdin_bytes),
                    daemon=True,
                )
            )
        for thread in readers:
            thread.start()
            threads.append(thread)
        if tree is not None:
            outcome = tree.wait(timeout=timeout, sweep_on_exit=True, grace_seconds=0.2)
            timed_out = outcome.state is process_tree.TreeState.TIMED_OUT
        else:
            try:
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                with contextlib.suppress(OSError):
                    proc.kill()
                with contextlib.suppress(OSError, subprocess.TimeoutExpired):
                    proc.wait(timeout=5)
    except BaseException:
        if tree is not None:
            with contextlib.suppress(OSError):
                tree.terminate(grace_seconds=0.2, kill_wait_seconds=5.0)
        else:
            with contextlib.suppress(OSError):
                proc.kill()
        with contextlib.suppress(OSError, subprocess.TimeoutExpired):
            proc.wait(timeout=5)
        raise
    finally:
        if tree is not None:
            with contextlib.suppress(OSError):
                tree.close()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(OSError, ValueError):
                    stream.close()
        for thread in threads:
            thread.join(timeout=2)
    stdout = _format_captured_stream(stdout_bytes, stdout_state["bytes"], MAX_OUTPUT)
    stderr = _format_captured_stream(stderr_bytes, stderr_state["bytes"], MAX_OUTPUT)
    if timed_out:
        return {
            "ok": False, "returncode": -1, "timed_out": True,
            "elapsed_ms": int((time.time() - started) * 1000),
            "stdout": stdout,
            "stderr": stderr,
            "command": [str(c) for c in cmd],
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "timed_out": False,
        "elapsed_ms": int((time.time() - started) * 1000),
        "stdout": stdout,
        "stderr": stderr,
        "command": [str(c) for c in cmd],
    }


def _format_result(title, data):
    lines = [
        title,
        "  command: %s" % json.dumps(data.get("command", []), ensure_ascii=False),
        "  ok: %s" % data.get("ok", False),
        "  returncode: %s" % data.get("returncode"),
        "  elapsed_ms: %s" % data.get("elapsed_ms", 0),
    ]
    if data.get("timed_out"):
        lines.append("  timed_out: true")
    if data.get("stdout"):
        lines.extend(["stdout:", data["stdout"].rstrip()])
    if data.get("stderr"):
        lines.extend(["stderr:", data["stderr"].rstrip()])
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------

def _git():
    return shutil.which("git") or "git"


# Variables that make git use another repository, index or object store than
# the tree that was checked, or inject config entries (``-c`` by environment).
_GIT_LOCATION_VARIABLES = frozenset({
    "GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_COMMON_DIR",
    "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_CONFIG_COUNT", "GIT_CONFIG_PARAMETERS",
})
_GIT_LOCATION_PREFIXES = ("GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")


def _is_git_location_variable(name):
    upper = name.upper()  # Windows environment names are case-insensitive
    return upper in _GIT_LOCATION_VARIABLES or upper.startswith(_GIT_LOCATION_PREFIXES)


def _git_env():
    env = _child_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    # Git must work on the tree that was checked, not one the environment names.
    for name in [key for key in env if _is_git_location_variable(key)]:
        del env[name]
    return env


def _neutralized_git_arguments(root, *, timeout, env):
    """``-c`` overrides that keep repository config from launching programs.

    ``acceptEdits`` lets these mutation-class git tools run unattended while
    running host programs still asks. Hooks, ``core.fsmonitor``, filter,
    merge and textconv drivers and signing programs are replaced by the
    shared :mod:`git_program_guard` list (also used by the read-only
    ``git_tools``). Raises ``ValueError`` when the driver configuration cannot
    be read, so the caller refuses rather than running git with a driver left
    in place.
    """
    overrides = git_program_guard.neutralized_git_arguments(
        lambda arguments: _run([_git()] + arguments, cwd=root, timeout=timeout, env=env),
    )
    # ``git add`` (git_commit) inspects populated submodules with a child git
    # that reads each submodule's own config; neutralize those drivers too.
    return overrides + git_program_guard.submodule_driver_overrides(
        lambda relative, arguments: _git_probe_at(
            root, relative, arguments, timeout=timeout, env=env,
        ),
    )


def _git_probe_at(root, relative, arguments, *, timeout, env):
    """Run one guard probe in the repository checked out at ``relative``.

    Unlike ``_run`` the output is not trimmed: the submodule listing is
    parsed, not displayed.  ``None`` means no repository is checked out there.
    """
    directory = Path(root) / relative if relative else Path(root)
    if relative and not (directory / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            [_git(), *arguments], cwd=str(directory), timeout=timeout,
            capture_output=True, stdin=subprocess.DEVNULL, env=env,
        )
    except subprocess.TimeoutExpired:
        return {"timed_out": True, "returncode": -1, "stdout": "", "stderr": ""}
    except FileNotFoundError:
        return {"returncode": -1, "stdout": "", "stderr": "git was not found"}
    return {
        "returncode": proc.returncode,
        "stdout": proc.stdout.decode("utf-8", errors="surrogateescape"),
        "stderr": proc.stderr.decode("utf-8", errors="replace"),
    }


def _git_change_runner(root, *, timeout, hardened):
    """A runner for ``git_change_targets``: git in ``root``, output untrimmed.

    ``hardened`` adds the same driver/hook overrides as ``_run_git`` (raising
    ``ValueError`` when they cannot be read); ``apply_patch`` runs a bare
    ``git apply`` and lists its paths the same way.
    """
    env = _git_env()
    overrides = _neutralized_git_arguments(root, timeout=timeout, env=env) if hardened else []

    def run(arguments, stdin_bytes=b""):
        proc = subprocess.run(
            [_git(), *overrides, *arguments], cwd=str(root), timeout=timeout,
            capture_output=True, input=stdin_bytes, env=env,
        )
        return proc.returncode, proc.stdout

    return run


def _live_refusal(operation, root):
    """``{"ok": False, "error": ...}`` when ``operation`` may not change the live runtime checkout at ``root``, else None.

    Decided from paths, git's ``rev-parse`` answer when they find nothing, and
    the operator-attendance signal (``live_checkout.refusal``).
    """
    refused = live_checkout.refusal(operation, Path(root))
    return refused.as_result() if refused is not None else None


def _patch_refusal(operation, root, patch_text, *, timeout):
    """``{"ok": False, "error": ...}`` when the patch would write a protected control-plane path, else None.

    ``git apply --numstat`` lists every path the patch writes; each gets the
    file tools' guard, in any repository (``git_change_targets.patch_refusal``).
    """
    timeout = _bounded_int(timeout, 10, 1, MAX_TIMEOUT)
    run = _git_change_runner(root, timeout=timeout, hardened=False)
    top = git_change_targets.toplevel(run)
    try:
        paths = git_change_targets.patch_targets(run, patch_text)
    except git_change_targets.TargetsUnknown as exc:
        return {"ok": False, "error": git_change_targets.unlisted_refusal(operation, exc)}
    reason = git_change_targets.patch_refusal(operation, root, top, paths)
    return {"ok": False, "error": reason} if reason else None


def _run_git(root, args, *, timeout=10):
    env = _git_env()
    try:
        overrides = _neutralized_git_arguments(root, timeout=timeout, env=env)
    except ValueError as exc:
        return {
            "ok": False, "returncode": -1, "timed_out": False, "elapsed_ms": 0,
            "stdout": "", "stderr": "refusing to run git: %s" % exc,
            "command": [_git()] + list(args),
        }
    return _run([_git()] + overrides + list(args), cwd=root, timeout=timeout, env=env)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

_TEST_FRAMEWORKS = {
    # The runtime venv is intentionally minimal on some installations.  The
    # command is selected at call time by _pytest_cmd(), which falls back to
    # the venv's base interpreter when the embedded pytest package is only a
    # namespace directory (no pytest.__main__).
    "pytest": {"cmd": [], "marker_files": ["pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py"]},
    "unittest": {"cmd": [sys.executable, "-m", "unittest", "discover"], "marker_files": []},
    "jest": {"cmd": ["npx", "jest"], "marker_files": ["jest.config.js", "jest.config.ts", "jest.config.mjs"]},
    "vitest": {"cmd": ["npx", "vitest", "run"], "marker_files": ["vitest.config.ts", "vitest.config.js", "vitest.config.mts"]},
    "mocha": {"cmd": ["npx", "mocha"], "marker_files": [".mocharc.yml", ".mocharc.json", ".mocharc.js"]},
    "bun": {"cmd": ["bun", "test"], "marker_files": ["bunfig.toml", "bun.lock", "bun.lockb"]},
    "node-test": {"cmd": ["node", "--test"], "marker_files": []},
    "cargo": {"cmd": ["cargo", "test"], "marker_files": ["Cargo.toml"]},
    "go": {"cmd": ["go", "test", "./..."], "marker_files": ["go.mod"]},
    "dotnet": {"cmd": ["dotnet", "test"], "marker_files": ["*.csproj", "*.fsproj", "*.vbproj", "*.sln"]},
    "dart": {"cmd": ["dart", "test"], "marker_files": ["pubspec.yaml"]},
    "flutter": {"cmd": ["flutter", "test"], "marker_files": []},
    "ctest": {"cmd": ["ctest", "--test-dir", "build", "--output-on-failure"], "marker_files": ["CMakeLists.txt"]},
    "zig": {"cmd": ["zig", "build", "test"], "marker_files": ["build.zig"]},
    "swift": {"cmd": ["swift", "test"], "marker_files": ["Package.swift"]},
    "maven": {"cmd": ["mvn", "test", "-q"], "marker_files": ["pom.xml"]},
    "gradle": {"cmd": ["./gradlew" if os.name != "nt" else "gradlew.bat", "test"], "marker_files": ["build.gradle", "build.gradle.kts"]},
    "elixir": {"cmd": ["mix", "test"], "marker_files": ["mix.exs"]},
    "phpunit": {"cmd": ["phpunit"], "marker_files": ["phpunit.xml", "phpunit.xml.dist", "composer.json"]},
    "rspec": {"cmd": ["bundle", "exec", "rspec"], "marker_files": [".rspec", "Gemfile"]},
}


def _pytest_cmd():
    """Return a Python interpreter that can actually launch pytest.

    Sonder's embedded ``.runtime`` may be present without a complete pytest
    installation.  ``import pytest`` then succeeds as a namespace package,
    but ``python -m pytest`` fails with ``pytest.__main__`` missing.  Prefer
    the runtime interpreter when healthy and use its base interpreter as a
    local, deterministic fallback; never install packages or invoke a shell.
    """
    candidates = [sys.executable]
    base = Path(sys.base_prefix)
    base_python = base / ("python.exe" if os.name == "nt" else "bin/python")
    if str(base_python) not in candidates and base_python.exists():
        candidates.append(str(base_python))
    for executable in candidates:
        result = _run(
            [executable, "-c", "import pytest.__main__"],
            cwd=Path.cwd(), timeout=5,
        )
        if result.get("ok"):
            return executable
    return sys.executable


def _detect_test_framework(root):
    root = Path(root)
    if (root / "pubspec.yaml").exists():
        try:
            pub_text = (root / "pubspec.yaml").read_text(encoding="utf-8", errors="replace")
            if re.search(r"(?m)^\s{0,4}flutter\s*:", pub_text):
                return "flutter"
        except OSError:
            pass
        return "dart"
    for name, info in _TEST_FRAMEWORKS.items():
        for marker in info["marker_files"]:
            if "*" in marker:
                if list(root.glob(marker)):
                    return name
            elif (root / marker).exists():
                return name
    if (root / "package.json").exists():
        try:
            pkg = json.loads((root / "package.json").read_text(encoding="utf-8"))
            scripts = pkg.get("scripts", {})
            if "test" in scripts:
                test_cmd = scripts["test"]
                for fw in ("vitest", "jest", "mocha"):
                    if fw in test_cmd:
                        return fw
        except (json.JSONDecodeError, OSError):
            pass
    return "pytest"


def test_discover(root=".", framework="auto", extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if framework == "auto":
        framework = _detect_test_framework(root)

    info = {"framework": framework, "root": str(root), "test_files": [], "test_count": 0}

    if framework == "pytest":
        result = _run(
            [_pytest_cmd(), "-m", "pytest", "--collect-only", "-q", "--no-header"],
            cwd=root, timeout=30,
        )
        if result["ok"]:
            lines = result["stdout"].strip().splitlines()
            tests = [l for l in lines if "::" in l]
            info["test_files"] = sorted(set(t.split("::")[0] for t in tests))
            info["test_count"] = len(tests)
        elif result.get("returncode") == 5:
            # pytest reserves exit code 5 for a completed collection that
            # found no tests. That is a valid zero-test inventory, not a
            # broken collector; callers need to distinguish it from syntax,
            # import, and configuration errors without guessing from text.
            info["no_tests"] = True
        else:
            info["error"] = result["stderr"] or result["stdout"]
    elif framework in ("jest", "vitest"):
        result = _run(
            ["npx", framework, "--listTests"] if framework == "jest"
            else ["npx", "vitest", "list", "--reporter=json"],
            cwd=root, timeout=30,
        )
        if result["ok"]:
            files = [l.strip() for l in result["stdout"].splitlines() if l.strip()]
            info["test_files"] = files
            info["test_count"] = len(files)
        else:
            info["error"] = result["stderr"] or result["stdout"]
    elif framework == "cargo":
        result = _run(["cargo", "test", "--", "--list"], cwd=root, timeout=30)
        if result["ok"]:
            tests = [l.split(":")[0] for l in result["stdout"].splitlines() if ": test" in l]
            info["test_count"] = len(tests)
        else:
            info["error"] = result["stderr"] or result["stdout"]
    elif framework == "go":
        result = _run(["go", "test", "./...", "-list", ".*"], cwd=root, timeout=30)
        if result["ok"]:
            tests = [l for l in result["stdout"].splitlines() if l.startswith("Test") or l.startswith("Benchmark")]
            info["test_count"] = len(tests)
        else:
            info["error"] = result["stderr"] or result["stdout"]

    return info


def test_run(
    root=".", framework="auto", path="", pattern="", verbose=False,
    coverage=False, timeout=120, extra_args_json="",
    extra_roots="",
):
    """Run a project's tests with a host-built command line.

    ``extra_args_json`` is retired: raw argv let a caller hand pytest ``-p``,
    ``-c`` or ``--rootdir`` and step around every host-owned template. It is
    kept in the signature only so a caller that still passes it gets a clear
    refusal instead of a silently dropped argument. Omitted, ``""`` and
    ``"[]"`` keep working.
    """
    refusal = retired_extra_args(extra_args_json)
    if refusal is not None:
        refusal["framework"] = framework
        return refusal
    root = _resolve_root(root, extra_roots)
    # Confine the argv-appended path too; see _resolve_target_path.
    path = _resolve_target_path(root, path)
    timeout = _bounded_int(timeout, 120, 5, MAX_TIMEOUT)
    if framework == "auto":
        framework = _detect_test_framework(root)

    base = list(_TEST_FRAMEWORKS.get(framework, {}).get("cmd", []))
    if framework == "pytest":
        base = [_pytest_cmd(), "-m", "pytest"]
    if framework == "gradle" and os.name == "nt":
        # The project's own wrapper, by absolute path: a bare name is a PATH/cwd lookup.
        base[0] = str(root / "gradlew.bat")
    if not base:
        return {"ok": False, "error": "unknown framework: %s" % framework, "framework": framework}

    cmd = list(base)

    if framework == "pytest":
        if verbose:
            cmd.append("-v")
        if coverage:
            cmd.extend(["--cov", "--cov-report=term-missing"])
        if pattern:
            cmd.extend(["-k", pattern])
        if path:
            cmd.append(path)
        cmd.extend(["--tb=short", "--no-header", "-q"])
    elif framework in ("jest", "vitest"):
        if verbose:
            cmd.append("--verbose")
        if coverage:
            cmd.append("--coverage")
        if pattern:
            cmd.extend(["-t", pattern])
        if path:
            cmd.append(path)
    elif framework == "cargo":
        if path:
            cmd.extend(["--test", path])
        if pattern:
            cmd.extend(["--", pattern])
    elif framework == "go":
        if path:
            cmd[-1] = path
        if pattern:
            cmd.extend(["-run", pattern])
        if verbose:
            cmd.append("-v")
        if coverage:
            cmd.append("-cover")

    result = _run(cmd, cwd=root, timeout=timeout)
    result["framework"] = framework
    return result


# ---------------------------------------------------------------------------
# Lint / Format / Type check
# ---------------------------------------------------------------------------

_LINT_TOOLS = {
    "ruff": {"check": ["ruff", "check"], "fix": ["ruff", "check", "--fix"]},
    "flake8": {"check": ["flake8"], "fix": None},
    # Runtime-local fallbacks.  Do not resolve these through another venv or
    # assume a global executable: the interpreter running the harness owns the
    # import/module lookup.
    # -I: with cwd=root a project-local pyflakes.py would shadow the module and
    # run under the runtime interpreter.
    "pyflakes": {"check": [sys.executable, "-I", "-m", "pyflakes"], "fix": None},
    "py_compile": {"check": [sys.executable, "-I", "-m", "compileall", "-q"], "fix": None},
    "pylint": {"check": ["pylint"], "fix": None},
    "eslint": {"check": ["npx", "eslint"], "fix": ["npx", "eslint", "--fix"]},
    "biome": {"check": ["npx", "@biomejs/biome", "lint"], "fix": ["npx", "@biomejs/biome", "lint", "--write"]},
    "clippy": {"check": ["cargo", "clippy"], "fix": ["cargo", "clippy", "--fix", "--allow-dirty"]},
    "go-vet": {"check": ["go", "vet", "./..."], "fix": None},
    "golangci-lint": {"check": ["golangci-lint", "run"], "fix": ["golangci-lint", "run", "--fix"]},
    "dart": {"check": ["dart", "analyze"], "fix": ["dart", "fix", "--apply"]},
    "dotnet": {"check": ["dotnet", "format", "--verify-no-changes"], "fix": ["dotnet", "format"]},
    "shellcheck": {"check": ["shellcheck"], "fix": None},
    "phpcs": {"check": ["phpcs"], "fix": ["phpcbf"]},
    "phpstan": {"check": ["phpstan", "analyse"], "fix": None},
    "rubocop": {"check": ["rubocop"], "fix": ["rubocop", "-A"]},
    "ktlint": {"check": ["ktlint"], "fix": ["ktlint", "-F"]},
    "swiftlint": {"check": ["swiftlint", "lint"], "fix": ["swiftlint", "--fix"]},
    "credo": {"check": ["mix", "credo"], "fix": None},
    "luacheck": {"check": ["luacheck"], "fix": None},
    "clang-tidy": {"check": ["clang-tidy"], "fix": None},
}

_FORMATTERS = {
    "ruff": ["ruff", "format"],
    "black": ["black"],
    "prettier": ["npx", "--no-install", "prettier", "--write"],
    "biome": ["npx", "--no-install", "@biomejs/biome", "format", "--write"],
    "rustfmt": ["cargo", "fmt"],
    "gofmt": ["gofmt", "-w"],
    "clang-format": ["clang-format", "-i"],
    "dart": ["dart", "format"],
    "dotnet": ["dotnet", "format"],
    "zig": ["zig", "fmt"],
    "swift-format": ["swift-format", "format", "-i"],
    "mix": ["mix", "format"],
    "shfmt": ["shfmt", "-w"],
    "stylua": ["stylua"],
    "ktlint": ["ktlint", "-F"],
    "rubocop": ["rubocop", "-A"],
}

# Read-only check form of every supported formatter: ``check_only=True`` uses
# this argv and never falls back to a writing command. Ruff and Black run from the
# runtime interpreter (``formatter_config_guard.python_formatter_command`` replaces
# the program name) without project configuration: ``--isolated`` for Ruff, an
# empty ``--config`` file outside the root for Black. Prettier never runs through
# npx: ``_prettier_check`` starts a trusted absolute Prettier with ``--no-config``.
# Biome, dotnet and rustfmt are refused in check mode by ``formatter_config_guard``.
_FORMATTER_CHECKS = {
    "ruff": ["ruff", "format", "--check", "--isolated", "--no-cache"],
    "black": ["black", "--check"],
    "prettier": ["prettier", "--check", "--no-config", "--no-editorconfig"],
    "rustfmt": ["cargo", "fmt", "--check"],
    "gofmt": ["gofmt", "-l"],
    "clang-format": ["clang-format", "--dry-run", "--Werror"],
    "dart": ["dart", "format", "--output=none", "--set-exit-if-changed"],
    "dotnet": ["dotnet", "format", "--verify-no-changes"],
}

_TYPECHECKERS = {
    # -I: prevent a project-local mypy.py from shadowing python -m mypy.
    "mypy": [sys.executable, "-I", "-m", "mypy"],
    "pyright": ["npx", "pyright"],
    "tsc": ["npx", "tsc", "--noEmit"],
    "dart": ["dart", "analyze"],
    "go-vet": ["go", "vet", "./..."],
    "cargo-check": ["cargo", "check"],
    "dotnet": ["dotnet", "build", "--no-incremental", "-v", "q"],
    "zig": ["zig", "ast-check"],
    "phpstan": ["phpstan", "analyse"],
    "sorbet": ["bundle", "exec", "srb", "tc"],
    "dialyzer": ["mix", "dialyzer"],
}


def _has_glob(root: Path, pattern: str) -> bool:
    return any(root.glob(pattern))


def _detect_linter(root):
    import importlib.util
    root = Path(root)
    ruff_available = bool(shutil.which("ruff") or importlib.util.find_spec("ruff"))
    if ruff_available and (root / "pyproject.toml").exists():
        return "ruff"
    if (root / "pubspec.yaml").exists() and shutil.which("dart"):
        return "dart"
    if (_has_glob(root, "*.sln") or _has_glob(root, "*.csproj")) and shutil.which("dotnet"):
        return "dotnet"
    if (root / "biome.json").exists():
        return "biome"
    if (root / ".eslintrc.js").exists() or (root / ".eslintrc.json").exists() or (root / "eslint.config.js").exists():
        return "eslint"
    if (root / "Cargo.toml").exists():
        return "clippy"
    if (root / "go.mod").exists():
        return "golangci-lint" if shutil.which("golangci-lint") else "go-vet"
    if (root / "mix.exs").exists():
        return "credo"
    if (root / "Package.swift").exists() and shutil.which("swiftlint"):
        return "swiftlint"
    if (root / "Gemfile").exists() and shutil.which("rubocop"):
        return "rubocop"
    if (root / "composer.json").exists():
        if shutil.which("phpstan"):
            return "phpstan"
        if shutil.which("phpcs"):
            return "phpcs"
    if ruff_available:
        return "ruff"
    if shutil.which("flake8"):
        return "flake8"
    try:
        import pyflakes  # noqa: F401
    except ImportError:
        return "py_compile"
    return "pyflakes"


def _detect_formatter(root):
    root = Path(root)
    if shutil.which("ruff") and (root / "pyproject.toml").exists():
        return "ruff"
    if (root / "pubspec.yaml").exists() and shutil.which("dart"):
        return "dart"
    if (_has_glob(root, "*.sln") or _has_glob(root, "*.csproj")) and shutil.which("dotnet"):
        return "dotnet"
    if (root / "build.zig").exists() and shutil.which("zig"):
        return "zig"
    if (root / "mix.exs").exists():
        return "mix"
    if (root / "Package.swift").exists() and shutil.which("swift-format"):
        return "swift-format"
    if (root / "biome.json").exists():
        return "biome"
    if ((root / ".clang-format").exists() or (root / "CMakeLists.txt").exists()) and shutil.which("clang-format"):
        return "clang-format"
    if shutil.which("black"):
        return "black"
    if (root / "package.json").exists():
        return "prettier"
    if (root / "Cargo.toml").exists():
        return "rustfmt"
    if (root / "go.mod").exists():
        return "gofmt"
    return "ruff"


def _detect_typechecker(root):
    root = Path(root)
    if (root / "tsconfig.json").exists():
        return "tsc"
    if (root / "pubspec.yaml").exists() and shutil.which("dart"):
        return "dart"
    if (root / "Cargo.toml").exists():
        return "cargo-check"
    if (root / "go.mod").exists():
        return "go-vet"
    if (_has_glob(root, "*.sln") or _has_glob(root, "*.csproj")) and shutil.which("dotnet"):
        return "dotnet"
    if (root / "composer.json").exists() and shutil.which("phpstan"):
        return "phpstan"
    if (root / "mix.exs").exists():
        return "dialyzer"
    if (root / "pyrightconfig.json").exists():
        return "pyright"
    if shutil.which("mypy"):
        return "mypy"
    return "mypy"


def lint_run(root=".", tool="auto", path="", fix=False, timeout=60, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    # Confine the argv-appended path too; see _resolve_target_path.
    path = _resolve_target_path(root, path)
    timeout = _bounded_int(timeout, 60, 5, MAX_TIMEOUT)
    if tool == "auto":
        tool = _detect_linter(root)
    info = _LINT_TOOLS.get(tool)
    if not info:
        return {"ok": False, "error": "unknown linter: %s" % tool, "tool": tool}
    if fix and info.get("fix"):
        live = _live_refusal("lint_run", root / path if path else root)
        if live:
            return {**live, "tool": tool, "mode": "fix"}
        refused = protected_write_scope.scope_refusal("lint_run fix", root, root / path if path else root)
        if refused:
            return {"ok": False, "error": refused, "tool": tool, "mode": "fix"}
        cmd = list(info["fix"])
    else:
        cmd = list(info["check"])
    if tool == "ruff" and not shutil.which("ruff"):
        cmd = [sys.executable, "-I", "-m", "ruff", *cmd[1:]]
    if path:
        cmd.append(path)
    elif tool in ("ruff", "flake8", "pylint", "pyflakes", "py_compile"):
        cmd.append(".")
    result = _run(cmd, cwd=root, timeout=timeout)
    result["tool"] = tool
    result["mode"] = "fix" if fix else "check"
    return result


_TEMP_INSIDE_ROOT = (
    "refusing to run: the system temp folder is inside the project root, so Sonder has no "
    "scratch folder outside it for the files it hands the tool")


@contextlib.contextmanager
def _outside_temp_dir(root, prefix, extra_roots=""):
    """Yield a fresh private temp folder outside ``root`` and every authorized root (None otherwise); remove it after.

    The folder is created by ``mkdtemp`` (owner-only on POSIX) and holds only
    files Sonder writes: an empty ignore or config file, or a sanitized
    requirements copy. Owned by the calling thread for the duration of the block.
    """
    folder = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        resolved = folder.resolve()
        if any(resolved == forbidden or file_ops._is_inside(resolved, forbidden)
               for forbidden in _formatter_forbidden_roots(root, extra_roots)):
            yield None
        else:
            yield resolved
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _formatter_forbidden_roots(root, extra_roots=""):
    """The project root plus every authorized root: no trusted formatter may live under one."""
    roots = [Path(root).resolve()]
    for allowed in file_ops.allowed_roots(extra_roots or _AUTHORIZED_ROOT.get()):
        if allowed not in roots:
            roots.append(allowed)
    return roots


def _prettier_check(root, path, timeout, extra_roots=""):
    """``prettier --check`` from a trusted absolute Prettier 3+ that reads no project configuration.

    Never npx: npx resolves ``node_modules/.bin`` and ``.npmrc`` from the working
    folder and every parent. The trusted executable (``SONDER_PRETTIER_EXE`` or
    PATH, never under the project or an authorized root) runs twice from a temp
    folder outside the root with a Node environment stripped of ``NODE_*`` and
    ``npm_config_*``: ``--version`` (Prettier 2 resolved plugins and module
    ``parser`` values on its own, so anything below 3 is refused), then
    ``--check --no-config --no-editorconfig --ignore-path <empty file>`` on the
    absolute target.
    """
    def refusal(reason):
        return {"ok": False, "error": "refusing to run prettier in check mode: " + reason,
                "tool": "prettier", "mode": "check"}

    forbidden = _formatter_forbidden_roots(root, extra_roots)
    executable, error = formatter_config_guard.resolve_vetted_prettier(forbidden)
    if error:
        return refusal(error)
    # Unlike gofmt/clang-format/dart/pip-audit, a batch shim is allowed here (the npm-global
    # ``prettier.cmd`` is the usual Windows install): it only ever runs from a temp folder outside
    # the root with ``env`` below, whose PATH holds no project, relative or empty entry and which
    # turns the current-folder program search off, so the commands the shim calls cannot resolve
    # to project files.
    env = formatter_config_guard.sanitized_node_environment(_child_env(), forbidden)
    target = root / path if path else root
    with _outside_temp_dir(root, "sonder-prettier-", extra_roots) as work:
        if work is None:
            return refusal(_TEMP_INSIDE_ROOT)
        probe = _run([executable, "--version"], cwd=work, timeout=min(timeout, 30), env=env)
        major = formatter_config_guard.prettier_major_version(probe.get("stdout", "")) if probe.get("ok") else None
        if major is None:
            return refusal("the trusted Prettier did not report a version; check mode needs Prettier %d or newer"
                           % formatter_config_guard.MIN_PRETTIER_MAJOR)
        if major < formatter_config_guard.MIN_PRETTIER_MAJOR:
            return refusal("the trusted Prettier reports major version %d; check mode needs Prettier %d or newer "
                           "(older versions load plugins and parser modules on their own)"
                           % (major, formatter_config_guard.MIN_PRETTIER_MAJOR))
        ignore = work / "empty.prettierignore"
        ignore.write_bytes(b"")
        result = _run(formatter_config_guard.prettier_check_argv(executable, ignore, target),
                      cwd=work, timeout=timeout, env=env)
    result["tool"] = "prettier"
    result["mode"] = "check"
    return result


def format_code(root=".", tool="auto", path="", check_only=False, timeout=60, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    # Confine the argv-appended path too; see _resolve_target_path.
    path = _resolve_target_path(root, path)
    timeout = _bounded_int(timeout, 60, 5, MAX_TIMEOUT)
    if tool == "auto":
        tool = _detect_formatter(root)
    cmd_template = _FORMATTERS.get(tool)
    if not cmd_template:
        return {"ok": False, "error": "unknown formatter: %s" % tool, "tool": tool}
    mode = "check" if check_only else "format"
    # Check mode gets no exemption: a formatter run loads project configuration
    # and writes caches whether or not it rewrites sources.
    live = _live_refusal("format_code", root / path if path else root)
    if live:
        return {**live, "tool": tool, "mode": mode}
    refused = protected_write_scope.scope_refusal("format_code", root, root / path if path else root)
    if refused:
        return {"ok": False, "error": refused, "tool": tool, "mode": mode}
    if check_only:
        config_refused = formatter_config_guard.executable_config_error(tool, root, path)
        if config_refused:
            return {"ok": False, "error": config_refused, "tool": tool, "mode": "check"}
        # Check mode must never fall back to a writing formatter command.
        check_template = _FORMATTER_CHECKS.get(tool)
        if not check_template:
            return {
                "ok": False,
                "error": "formatter has no check-only mode: %s" % tool,
                "tool": tool,
                "mode": "check",
            }
        if tool == "prettier":
            return _prettier_check(root, path, timeout, extra_roots)
        cmd = list(check_template)
        if tool in ("ruff", "black"):
            prefix, prefix_err = formatter_config_guard.python_formatter_command(
                tool, _formatter_forbidden_roots(root, extra_roots))
            if prefix_err:
                return {"ok": False, "error": prefix_err, "tool": tool, "mode": "check"}
            cmd = prefix + cmd[1:]
        else:
            # A bare name (gofmt, clang-format, dart) is resolved here, never by the child
            # launch: only an absolute file in a PATH folder outside the project and every
            # authorized root runs.
            resolved = formatter_config_guard.vetted_which(
                cmd[0], _formatter_forbidden_roots(root, extra_roots))
            if not resolved:
                return {
                    "ok": False,
                    "error": "refusing to run %s in check mode: no %s on PATH outside the project and "
                             "every authorized root (the current folder is never searched)" % (tool, cmd[0]),
                    "tool": tool,
                    "mode": "check",
                }
            batch = formatter_config_guard.batch_shim_error(resolved, tool)
            if batch:
                return {"ok": False, "error": batch, "tool": tool, "mode": "check"}
            cmd[0] = resolved
    else:
        cmd = list(cmd_template)
    if path:
        target_args = [path]
    elif tool in ("ruff", "black", "dart") or (check_only and tool == "gofmt"):
        # ``gofmt -l`` with no path reads empty stdin instead of walking the root.
        target_args = ["."]
    elif check_only and tool == "clang-format":
        # ``clang-format --dry-run`` has no directory mode and exits 0 on empty stdin.
        return {
            "ok": False,
            "error": "path required: clang-format checks named files only",
            "tool": tool,
            "mode": "check",
        }
    else:
        target_args = []
    with contextlib.ExitStack() as scratch:
        if check_only and tool == "black":
            # An explicit, empty config: Black reads no pyproject.toml from the project.
            work = scratch.enter_context(_outside_temp_dir(root, "sonder-black-", extra_roots))
            if work is None:
                return {"ok": False, "error": _TEMP_INSIDE_ROOT, "tool": tool, "mode": "check"}
            config = work / "black-empty.toml"
            config.write_bytes(b"")
            cmd += ["--config", str(config)]
        # Known limit: a real executable started with cwd=root can still load a DLL that sits in
        # the project folder (the loader's own search order); the program, PATH and cmd.exe
        # lookups are closed above. See docs/wiki/03-configuration.md.
        check_env = (
            formatter_config_guard.check_mode_environment(
                _child_env(), _formatter_forbidden_roots(root, extra_roots))
            if check_only else None)
        result = _run(cmd + target_args, cwd=root, timeout=timeout, env=check_env)
    if check_only and tool == "gofmt" and result.get("ok") and result.get("stdout", "").strip():
        # ``gofmt -l`` lists files needing formatting and still exits 0.
        result["ok"] = False
    result["tool"] = tool
    result["mode"] = "check" if check_only else "format"
    return result


def typecheck_run(root=".", tool="auto", path="", timeout=120, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    # Confine the argv-appended path too; see _resolve_target_path.
    path = _resolve_target_path(root, path)
    timeout = _bounded_int(timeout, 120, 5, MAX_TIMEOUT)
    if tool == "auto":
        tool = _detect_typechecker(root)
    cmd_template = _TYPECHECKERS.get(tool)
    if not cmd_template:
        return {"ok": False, "error": "unknown type checker: %s" % tool, "tool": tool}
    cmd = list(cmd_template)
    if path:
        cmd.append(path)
    elif tool == "mypy":
        cmd.append(".")
    result = _run(cmd, cwd=root, timeout=timeout)
    result["tool"] = tool
    return result


# ---------------------------------------------------------------------------
# Dependency management
# ---------------------------------------------------------------------------

def _detect_package_manager(root):
    root = Path(root)
    if (root / "Cargo.toml").exists():
        return "cargo"
    if (root / "go.mod").exists():
        return "go"
    if (root / "uv.lock").exists() and shutil.which("uv"):
        return "uv"
    if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists():
        return "pip"
    if (root / "pubspec.yaml").exists():
        try:
            if re.search(r"(?m)^\s{0,4}flutter\s*:", (root / "pubspec.yaml").read_text(encoding="utf-8", errors="replace")):
                return "flutter"
        except OSError:
            pass
        return "dart"
    if _has_glob(root, "*.csproj") or _has_glob(root, "*.fsproj") or _has_glob(root, "*.sln"):
        return "dotnet"
    if (root / "bun.lock").exists() or (root / "bun.lockb").exists():
        return "bun"
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "package.json").exists():
        return "npm"
    if (root / "composer.json").exists():
        return "composer"
    if (root / "Gemfile").exists():
        return "bundler"
    if (root / "mix.exs").exists():
        return "mix"
    if (root / "Package.swift").exists():
        return "swift"
    return "pip"


_SAFE_PACKAGE_SPEC_RE = re.compile(r"^[A-Za-z0-9@][A-Za-z0-9._/@:^~<>=!,+\-\[\]*]*$")
_DISALLOWED_PACKAGE_PREFIXES = ("file:", "git+", "hg+", "svn+", "bzr+", "http:", "https:", "ssh:")


def _parse_and_validate_packages(packages_json, *, allow_empty=False):
    """Parse and validate a JSON list of safe package specifiers (no flag/URL/path injection)."""
    if allow_empty and (packages_json is None or str(packages_json).strip() == ""):
        return [], None
    try:
        packages = json.loads(packages_json)
    except (json.JSONDecodeError, TypeError):
        return None, "invalid JSON in packages_json"
    if not isinstance(packages, list) or (not allow_empty and not packages):
        return None, "packages_json must be a non-empty JSON array of package names"
    cleaned = []
    for item in packages:
        if not isinstance(item, str):
            return None, "each package in packages_json must be a non-empty string"
        spec = item.strip()
        if not spec or spec != item:
            return None, "invalid package specifier: %r" % item
        if spec.startswith("-"):
            return None, "package specifier must not start with '-': %r" % spec
        lowered = spec.lower()
        if (
            "://" in spec
            or lowered.startswith(_DISALLOWED_PACKAGE_PREFIXES)
            or ".." in spec
            or "\\" in spec
            or spec.startswith("/")
            or any(ch in spec for ch in ("\0", "\r", "\n"))
            or re.search(r"\s", spec)
            or not _SAFE_PACKAGE_SPEC_RE.fullmatch(spec)
        ):
            return None, "unsafe package specifier: %r" % spec
        cleaned.append(spec)
    return cleaned, None


_PROJECT_VENV_DIRECTORIES = (".venv", "venv")


def _project_site_packages(root):
    """The ``site-packages`` folder of the project's own venv under ``root``, or None.

    Nothing inside the repository is ever executed: the venv is only a target
    directory for Sonder's own pip (see ``project_venv``). The runtime's own
    environment never qualifies.
    """
    root = Path(root)
    try:
        runtime_key = os.path.normcase(str(Path(sys.prefix).resolve()))
    except OSError:
        runtime_key = os.path.normcase(str(Path(sys.prefix)))
    for name in _PROJECT_VENV_DIRECTORIES:
        venv = root / name
        try:
            if os.path.normcase(str(venv.resolve())) == runtime_key:
                continue
        except (OSError, RuntimeError, ValueError):
            continue
        target = project_venv.venv_site_packages(root, venv)
        if target is not None:
            return target
    return None


def _project_pip(root, action):
    """``([sys.executable, "-I", "-m", "pip", action, "--target", site-packages], None)`` or ``(None, error)``.

    Sonder's own interpreter runs pip and writes into the project venv's
    site-packages; the repository's interpreter, ``.pth`` files and committed pip
    are never executed. ``pip uninstall`` cannot target another folder, so
    removal is refused rather than run against the runtime environment.
    """
    if action == "uninstall":
        return None, {
            "ok": False,
            "error": "pip uninstall cannot target a project virtual environment without running its "
                     "interpreter, which is repo-controlled code; remove the package by hand",
            "manager": "pip",
        }
    target = _project_site_packages(root)
    if target is None:
        return None, {
            "ok": False,
            "error": "no project virtual environment (.venv or venv with pyvenv.cfg and a real "
                     "site-packages) under the root; refusing to run pip in the Sonder runtime's own interpreter",
            "manager": "pip",
        }
    return [sys.executable, "-I", "-m", "pip", action, "--target", str(target)], None


_NO_PEP508_PARSER = (
    "refusing requirements.txt: neither packaging nor pip's vendored copy is importable, so "
    "requirements cannot be parsed strictly")


def _parse_requirement(text):
    """Strict PEP 508 parse for ``requirements_guard``; ``ValueError`` when ``text`` is not one requirement."""
    if _PEP508Requirement is None:
        raise ValueError("no PEP 508 parser is available")
    return _PEP508Requirement(text)


def _requirements_link_error(root):
    """Return an error message if ``root/requirements.txt`` may not be handed to pip, else None.

    The top-level file must not be a link escaping root, and the file plus every
    ``-r``/``-c`` include must pass ``requirements_guard`` (no escaping or linked
    includes, URLs, editables, index overrides or direct references).
    """
    req_path = Path(root) / "requirements.txt"
    if file_ops.is_reparse_point(req_path) or (
        req_path.exists() and _secret_scan_adapter.confined_path(req_path, Path(root)) is None
    ):
        return "refusing to read requirements.txt: it is a link or resolves outside the root"
    if _PEP508Requirement is None and os.path.lexists(req_path):
        return _NO_PEP508_PARSER
    content = requirements_guard.requirements_content_error(
        root, is_protected=_is_protected_read_target, parse=_parse_requirement)
    if content:
        return "refusing requirements.txt: %s" % content
    return None


@contextlib.contextmanager
def _sanitized_requirements_file(root, extra_roots=""):
    """Yield ``(path, None)`` for a sanitized flat copy of ``root/requirements.txt``, or ``(None, error)``.

    pip and pip-audit never see the repository's pathname: the file and every
    include are read once through ``confined_read`` and validated by
    ``requirements_guard``, and only the validated, canonical lines are written
    to a fresh temp folder outside the root, which is removed when the block
    ends. Callers run the child with ``cwd`` set to that folder, so nothing is
    resolved against the project either.
    """
    req_path = Path(root) / "requirements.txt"
    if file_ops.is_reparse_point(req_path) or (
        req_path.exists() and _secret_scan_adapter.confined_path(req_path, Path(root)) is None
    ):
        yield None, "refusing to read requirements.txt: it is a link or resolves outside the root"
        return
    if _PEP508Requirement is None:
        yield None, _NO_PEP508_PARSER
        return
    sanitized, error = requirements_guard.sanitized_requirements(
        root, is_protected=_is_protected_read_target, parse=_parse_requirement)
    if sanitized is None:
        yield None, "refusing requirements.txt: %s" % error
        return
    with _outside_temp_dir(root, "sonder-requirements-", extra_roots) as work:
        if work is None:
            yield None, _TEMP_INSIDE_ROOT
        else:
            yield sanitized.write(work), None


def dependency_add(root=".", packages_json="[]", dev=False, timeout=60, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 60, 5, MAX_TIMEOUT)
    packages, err = _parse_and_validate_packages(packages_json, allow_empty=False)
    if err:
        return {"ok": False, "error": err}
    live = _live_refusal("dependency_add", root)
    if live:
        return live
    refused = protected_write_scope.scope_refusal("dependency_add", root, root)
    if refused:
        return {"ok": False, "error": refused}

    mgr = _detect_package_manager(root)
    if mgr == "pip":
        pip, pip_err = _project_pip(root, "install")
        if pip_err:
            return pip_err
        cmd = pip + ["--"] + packages
    elif mgr == "uv":
        cmd = ["uv", "add"] + (["--dev"] if dev else []) + ["--"] + packages
    elif mgr == "npm":
        cmd = ["npm", "install"] + (["--save-dev"] if dev else []) + packages
    elif mgr == "pnpm":
        cmd = ["pnpm", "add"] + (["-D"] if dev else []) + packages
    elif mgr == "yarn":
        cmd = ["yarn", "add"] + (["--dev"] if dev else []) + packages
    elif mgr == "bun":
        cmd = ["bun", "add"] + (["-d"] if dev else []) + packages
    elif mgr == "cargo":
        cmd = ["cargo", "add"] + (["--dev"] if dev else []) + ["--"] + packages
    elif mgr == "go":
        cmd = ["go", "get"] + packages
    elif mgr == "dotnet":
        cmd = ["dotnet", "add", "package", packages[0]]
    elif mgr in ("dart", "flutter"):
        cmd = [mgr, "pub", "add"] + ([f"dev:{p}" for p in packages] if dev else packages)
    elif mgr == "composer":
        cmd = ["composer", "require"] + (["--dev"] if dev else []) + ["--"] + packages
    elif mgr == "bundler":
        cmd = ["bundle", "add"] + packages
    else:
        return {"ok": False, "error": "unsupported package manager: %s" % mgr}

    result = _run(cmd, cwd=root, timeout=timeout)
    result["manager"] = mgr
    result["packages"] = packages
    return result


def dependency_remove(root=".", packages_json="[]", timeout=60, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 60, 5, MAX_TIMEOUT)
    packages, err = _parse_and_validate_packages(packages_json, allow_empty=False)
    if err:
        return {"ok": False, "error": err}
    live = _live_refusal("dependency_remove", root)
    if live:
        return live
    refused = protected_write_scope.scope_refusal("dependency_remove", root, root)
    if refused:
        return {"ok": False, "error": refused}

    mgr = _detect_package_manager(root)
    if mgr == "pip":
        pip, pip_err = _project_pip(root, "uninstall")
        if pip_err:
            return pip_err
        cmd = pip + ["-y", "--"] + packages
    elif mgr == "uv":
        cmd = ["uv", "remove", "--"] + packages
    elif mgr == "npm":
        cmd = ["npm", "uninstall"] + packages
    elif mgr == "pnpm":
        cmd = ["pnpm", "remove"] + packages
    elif mgr == "yarn":
        cmd = ["yarn", "remove"] + packages
    elif mgr == "bun":
        cmd = ["bun", "remove"] + packages
    elif mgr == "cargo":
        cmd = ["cargo", "remove", "--"] + packages
    elif mgr == "dotnet":
        cmd = ["dotnet", "remove", "package", packages[0]]
    elif mgr in ("dart", "flutter"):
        cmd = [mgr, "pub", "remove"] + packages
    elif mgr == "composer":
        cmd = ["composer", "remove", "--"] + packages
    elif mgr == "bundler":
        cmd = ["bundle", "remove"] + packages
    else:
        return {"ok": False, "error": "unsupported for remove: %s" % mgr}

    result = _run(cmd, cwd=root, timeout=timeout)
    result["manager"] = mgr
    result["packages"] = packages
    return result


def dependency_update(root=".", packages_json="[]", timeout=120, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 120, 5, MAX_TIMEOUT)
    packages, err = _parse_and_validate_packages(packages_json, allow_empty=True)
    if err:
        return {"ok": False, "error": err}
    live = _live_refusal("dependency_update", root)
    if live:
        return live
    refused = protected_write_scope.scope_refusal("dependency_update", root, root)
    if refused:
        return {"ok": False, "error": refused}

    mgr = _detect_package_manager(root)
    if mgr == "pip":
        pip, pip_err = _project_pip(root, "install")
        if pip_err:
            return pip_err
        if packages:
            cmd = pip + ["--upgrade", "--"] + packages
        else:
            if not os.path.lexists(root / "requirements.txt"):
                return {"ok": False, "error": "no requirements.txt to update; name the packages to upgrade",
                        "manager": "pip"}
            with _sanitized_requirements_file(root, extra_roots) as (sanitized, req_err):
                if req_err:
                    return {"ok": False, "error": req_err, "manager": "pip"}
                result = _run(pip + ["--upgrade", "-r", str(sanitized)], cwd=sanitized.parent, timeout=timeout)
            result["manager"] = mgr
            return result
    elif mgr == "uv":
        cmd = ["uv", "lock", "--upgrade"]
    elif mgr == "npm":
        cmd = ["npm", "update"] + packages
    elif mgr == "pnpm":
        cmd = ["pnpm", "update"] + packages
    elif mgr == "yarn":
        cmd = ["yarn", "upgrade"] + packages
    elif mgr == "bun":
        cmd = ["bun", "update"] + packages
    elif mgr == "cargo":
        cmd = ["cargo", "update"] + (["--package"] + packages if packages else [])
    elif mgr == "go":
        cmd = ["go", "get", "-u"] + (packages or ["./..."])
    elif mgr in ("dart", "flutter"):
        cmd = [mgr, "pub", "upgrade"] + packages
    elif mgr == "composer":
        cmd = ["composer", "update"] + (["--"] + packages if packages else [])
    elif mgr == "bundler":
        cmd = ["bundle", "update"] + packages
    elif mgr == "mix":
        cmd = ["mix", "deps.update"] + (packages if packages else ["--all"])
    elif mgr == "swift":
        cmd = ["swift", "package", "update"]
    else:
        return {"ok": False, "error": "unsupported for update: %s" % mgr}

    result = _run(cmd, cwd=root, timeout=timeout)
    result["manager"] = mgr
    return result


def dependency_audit(root=".", timeout=60, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 60, 5, MAX_TIMEOUT)
    mgr = _detect_package_manager(root)
    if mgr == "pip":
        # ``pip audit`` is not a pip command, and ``pip check`` audits Sonder's
        # own runtime environment. Use ``pip-audit --no-deps --disable-pip`` on
        # a sanitized copy of ``requirements.txt`` (see ``_sanitized_requirements_file``)
        # so sdist build hooks never run and pip-audit never reopens a repository file.
        # The runtime's own ``python -I -m pip_audit`` or an absolute PATH executable outside the
        # project and every authorized root; never a bare-name lookup (the current folder and a
        # project venv on PATH would win it).
        pip_audit, pip_audit_error = formatter_config_guard.pip_audit_command(
            _formatter_forbidden_roots(root, extra_roots))
        if os.path.lexists(root / "requirements.txt"):
            with _sanitized_requirements_file(root, extra_roots) as (sanitized, req_err):
                if req_err:
                    result = {"ok": False, "error": req_err}
                elif not pip_audit:
                    result = {"ok": False, "error": pip_audit_error}
                else:
                    result = _run(
                        pip_audit + ["-r", str(sanitized), "--no-deps", "--disable-pip"],
                        cwd=sanitized.parent, timeout=timeout,
                        env=formatter_config_guard.check_mode_environment(
                            _child_env(), _formatter_forbidden_roots(root, extra_roots)),
                    )
        elif not pip_audit:
            result = {"ok": False, "error": pip_audit_error}
        elif (root / "pyproject.toml").is_file():
            result = {
                "ok": False,
                "error": "pip-audit audits a pyproject.toml only by installing its dependencies, "
                         "which can run package build code; add a pinned requirements.txt",
            }
        else:
            result = {"ok": False, "error": "no requirements.txt to audit"}
    elif mgr in ("npm", "pnpm", "yarn"):
        cmd = ["npm", "audit", "--json"] if mgr == "npm" else [mgr, "audit"]
        result = _run(cmd, cwd=root, timeout=timeout)
    elif mgr == "cargo":
        if shutil.which("cargo-audit"):
            result = _run(["cargo", "audit"], cwd=root, timeout=timeout)
        else:
            result = {"ok": False, "error": "cargo-audit not installed; run: cargo install cargo-audit"}
    elif mgr == "dotnet":
        result = _run(["dotnet", "list", "package", "--vulnerable"], cwd=root, timeout=timeout)
    elif mgr in ("dart", "flutter"):
        result = _run([mgr, "pub", "outdated"], cwd=root, timeout=timeout)
    elif mgr == "composer":
        result = _run(["composer", "audit"], cwd=root, timeout=timeout)
    elif mgr == "go":
        if shutil.which("govulncheck"):
            result = _run(["govulncheck", "./..."], cwd=root, timeout=timeout)
        else:
            result = _run(["go", "vet", "./..."], cwd=root, timeout=timeout)
    else:
        result = {"ok": False, "error": "no audit support for %s" % mgr}
    result["manager"] = mgr
    return result


# ---------------------------------------------------------------------------
# Git mutations
# ---------------------------------------------------------------------------

def _guarded_git_mutation(operation):
    """Serialize host-driven Git mutations per working tree (Issue #510).

    A refused mutation returns the same ``{"ok": False, "error": ...}`` shape
    as any other Git failure, plus guard fields, and never runs Git.
    """
    def decorate(function):
        def guarded(root=".", *args, **kwargs):
            extra_roots = kwargs.get("extra_roots", "")
            try:
                key_root = _resolve_root(root, extra_roots)
            except Exception:
                # Let the unguarded function report its own path error.
                return function(root, *args, **kwargs)
            try:
                with guard_git_mutation(key_root, operation):
                    return function(root, *args, **kwargs)
            except ConcurrentGitMutation as refusal:
                return refusal.as_result()
        guarded.__name__ = function.__name__
        guarded.__qualname__ = function.__qualname__
        guarded.__doc__ = function.__doc__
        guarded.__wrapped__ = function
        return guarded
    return decorate


@_guarded_git_mutation("git_commit")
def git_commit(root=".", message="", paths_json="[]", all_tracked=False, timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not message:
        return {"ok": False, "error": "commit message is required"}
    try:
        paths = json.loads(paths_json)
        if not isinstance(paths, list):
            paths = []
    except json.JSONDecodeError:
        paths = []
    refused = _live_refusal("git_commit", root)
    if refused:
        return refused
    if paths:
        add_result = _run_git(root, ["add", "--"] + paths, timeout=timeout)
        if not add_result["ok"]:
            return add_result
    elif all_tracked:
        add_result = _run_git(root, ["add", "-u"], timeout=timeout)
        if not add_result["ok"]:
            return add_result

    result = _run_git(root, ["commit", "-m", message], timeout=timeout)
    return result


def _invalid_git_ref_error(value, label):
    if not isinstance(value, str):
        return "invalid git %s: must be a string" % label
    text = value.strip()
    if text.startswith("-") or any(ch in text for ch in ("\0", "\r", "\n")):
        return "invalid git %s: %r (must not start with '-')" % (label, text)
    return None


@_guarded_git_mutation("git_branch")
def git_branch(root=".", name="", checkout=True, base="", timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not name:
        return {"ok": False, "error": "branch name is required"}
    err = _invalid_git_ref_error(name, "branch name")
    if err:
        return {"ok": False, "error": err}
    if base:
        err = _invalid_git_ref_error(base, "base ref")
        if err:
            return {"ok": False, "error": err}
    refused = _live_refusal("git_branch", root)
    if refused:
        return refused
    if checkout:
        cmd = ["checkout", "-b", name]
    else:
        cmd = ["branch", "--", name]
    if base:
        cmd.append(base)
    return _run_git(root, cmd, timeout=timeout)


@_guarded_git_mutation("git_checkout")
def git_checkout(root=".", ref="", timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not ref:
        return {"ok": False, "error": "ref is required (branch name, tag, or commit)"}
    err = _invalid_git_ref_error(ref, "ref")
    if err:
        return {"ok": False, "error": err}
    refused = _live_refusal("git_checkout", root)
    if refused:
        return refused
    # Trailing ``--`` forces git to treat ``ref`` as a tree-ish/branch rather than
    # reverting working tree paths (for example ``git checkout .``).
    return _run_git(root, ["checkout", ref, "--"], timeout=timeout)


@_guarded_git_mutation("git_stash")
def git_stash(root=".", action="push", message="", include_untracked=True, timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if action == "push":
        cmd = ["stash", "push"]
        if include_untracked:
            cmd.append("-u")
        if message:
            cmd.extend(["-m", message])
    elif action == "pop":
        cmd = ["stash", "pop"]
    elif action == "list":
        cmd = ["stash", "list"]
    elif action == "drop":
        cmd = ["stash", "drop"]
    else:
        return {"ok": False, "error": "unknown stash action: %s (use push/pop/list/drop)" % action}
    if action in ("push", "pop", "drop"):
        refused = _live_refusal("git_stash", root)
        if refused:
            return refused
    if action in ("pop", "drop"):
        refused = _recovery_stash_refusal(action, root, timeout=timeout)
        if refused:
            return refused
    return _run_git(root, cmd, timeout=timeout)


def _recovery_stash_refusal(action, root, *, timeout):
    """``{"ok": False, ...}`` when ``git stash <action>`` would take the runtime recovery stash, else None.

    ``pop`` and ``drop`` act on ``stash@{0}``.  When that is a stash
    ``runtime_source_stash save`` recorded (in any worktree, since the stash
    list is shared), only ``runtime_source_stash pop`` may take it: it applies
    by commit id, needs the operator at the console and keeps its ledger in
    step.  Refused whoever is at the console, and refused when git cannot
    answer (a timeout, or configuration it cannot read).  The check and the
    git run are two steps.
    """
    probe_timeout = _bounded_int(timeout, 10, 1, git_tools.MAX_TIMEOUT)
    try:
        commit = git_tools.recorded_recovery_stash_at(root, timeout=probe_timeout)
    except (OSError, ValueError, PermissionError, TimeoutError) as exc:
        return {
            "ok": False,
            "error": "refusing git stash %s: could not check for the runtime recovery stash: %s" % (action, exc),
            "guard": RECOVERY_STASH_GUARD,
        }
    if not commit:
        return None
    return {
        "ok": False,
        "error": "refusing git stash %s: stash@{0} (%s) is the runtime recovery stash; restore it with "
                 "runtime_source_stash action=pop at the console" % (action, commit),
        "guard": RECOVERY_STASH_GUARD,
    }


@_guarded_git_mutation("git_tag")
def git_tag(root=".", name="", message="", delete=False, timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not name:
        return {"ok": False, "error": "tag name is required"}
    err = _invalid_git_ref_error(name, "tag name")
    if err:
        return {"ok": False, "error": err}
    refused = _live_refusal("git_tag", root)
    if refused:
        return refused
    if delete:
        cmd = ["tag", "-d", "--", name]
    elif message:
        cmd = ["tag", "-a", "-m", message, "--", name]
    else:
        cmd = ["tag", "--", name]
    return _run_git(root, cmd, timeout=timeout)


@_guarded_git_mutation("git_merge")
def git_merge(root=".", branch="", no_ff=True, message="", timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not branch:
        return {"ok": False, "error": "branch name is required"}
    err = _invalid_git_ref_error(branch, "branch name")
    if err:
        return {"ok": False, "error": err}
    cmd = ["merge"]
    if no_ff:
        cmd.append("--no-ff")
    if message:
        cmd.extend(["-m", message])
    cmd.append(branch)
    refused = _live_refusal("git_merge", root)
    if refused:
        return refused
    return _run_git(root, cmd, timeout=timeout)


@_guarded_git_mutation("git_cherry_pick")
def git_cherry_pick(root=".", commits_json="[]", timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    try:
        commits = json.loads(commits_json)
        if not isinstance(commits, list) or not commits:
            return {"ok": False, "error": "commits_json must be a non-empty JSON array of commit SHAs"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid JSON"}
    for commit in commits:
        if not isinstance(commit, str) or not commit.strip():
            return {"ok": False, "error": "each commit in commits_json must be a non-empty string"}
        err = _invalid_git_ref_error(commit, "commit")
        if err:
            return {"ok": False, "error": err}
    refused = _live_refusal("git_cherry_pick", root)
    if refused:
        return refused
    return _run_git(root, ["cherry-pick"] + commits, timeout=timeout)


# ---------------------------------------------------------------------------
# Build tools
# ---------------------------------------------------------------------------

_BUILD_RUN_SHELL_METACHAR_RE = re.compile(r"[&|;<>`$^\r\n()\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BUILD_RUN_BATCH_SPECIAL_RE = re.compile(r"[%!&|;<>^()]")


def _parse_build_run_command(command):
    """Validate and split a ``build_run`` command string without invoking a shell."""
    raw = str(command or "").strip()
    if not raw:
        return None, "build_run command is empty"
    if any(ch.isspace() and ch not in (" ", "\t") for ch in raw) or _BUILD_RUN_SHELL_METACHAR_RE.search(raw):
        return None, "refusing shell metacharacters or control characters in build_run command"
    plan = _interpreter_argv.analyze_build_command(
        raw, stdin_present=False, is_windows=(os.name == "nt"),
    )
    if plan.opaque_reason:
        return None, "refusing build_run command: %s" % plan.opaque_reason
    if plan.launcher:
        return None, "refusing command-string launcher %r in build_run command" % plan.launcher
    if plan.inline_violation:
        return None, "refusing inline interpreter execution in build_run command"
    if plan.stdin_violation:
        return None, "refusing stdin interpreter execution in build_run command"
    try:
        parts = shlex.split(raw, posix=(os.name != "nt"))
    except ValueError as exc:
        return None, "invalid build_run command syntax: %s" % exc
    if os.name == "nt":
        cleaned = []
        for token in parts:
            if len(token) >= 2 and token[0] == token[-1] and token[0] in ('"', "'"):
                token = token[1:-1]
            cleaned.append(token)
        parts = cleaned
    if not parts:
        return None, "build_run command is empty"
    return parts, ""


def build_run(root=".", command="", timeout=120, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 120, 5, MAX_TIMEOUT)

    if command:
        parts, err = _parse_build_run_command(command)
        if err:
            return {"ok": False, "error": err, "command": []}
        if os.name == "nt":
            prog = parts[0]
            if prog.lower() == "echo":
                resolved_echo = shutil.which("echo")
                if resolved_echo and Path(resolved_echo).suffix.lower() not in (".cmd", ".bat"):
                    result = _run([resolved_echo, *parts[1:]], cwd=root, timeout=timeout)
                    result["command"] = parts
                    return result
                return {
                    "ok": True,
                    "returncode": 0,
                    "timed_out": False,
                    "elapsed_ms": 0,
                    "stdout": " ".join(parts[1:]) + "\n",
                    "stderr": "",
                    "command": parts,
                }
            exec_parts = list(parts)
            if not any(sep in prog for sep in ("/", "\\")):
                resolved = shutil.which(prog)
                if resolved:
                    if Path(resolved).suffix.lower() in (".cmd", ".bat") and any(
                        _BUILD_RUN_BATCH_SPECIAL_RE.search(tok) for tok in parts
                    ):
                        return {
                            "ok": False,
                            "error": "refusing shell metacharacters in batch build_run command",
                            "command": parts,
                        }
                    exec_parts[0] = resolved
            result = _run(exec_parts, cwd=root, timeout=timeout)
            result["command"] = parts
            return result
        return _run(parts, cwd=root, timeout=timeout)

    if (root / "Makefile").exists():
        return _run(["make"], cwd=root, timeout=timeout)
    if (root / "Cargo.toml").exists():
        return _run(["cargo", "build"], cwd=root, timeout=timeout)
    if (root / "CMakeLists.txt").exists():
        return _run(["cmake", "--build", "build"], cwd=root, timeout=timeout)
    if (root / "meson.build").exists():
        return _run(["meson", "compile", "-C", "build"], cwd=root, timeout=timeout)
    if (root / "build.zig").exists():
        return _run(["zig", "build"], cwd=root, timeout=timeout)
    if (root / "Package.swift").exists():
        return _run(["swift", "build"], cwd=root, timeout=timeout)
    if (root / "mix.exs").exists():
        return _run(["mix", "compile"], cwd=root, timeout=timeout)
    if (root / "WORKSPACE").exists() or (root / "MODULE.bazel").exists():
        return _run(["bazel", "build", "//..."], cwd=root, timeout=timeout)
    if (root / "SConstruct").exists():
        return _run(["scons"], cwd=root, timeout=timeout)
    dotnet_targets = sorted(root.glob("*.sln")) or sorted(root.glob("*.csproj")) or sorted(root.glob("*.fsproj"))
    if dotnet_targets:
        return _run(["dotnet", "build", dotnet_targets[0].name], cwd=root, timeout=timeout)
    if (root / "pubspec.yaml").exists():
        if shutil.which("flutter"):
            return _run(["flutter", "analyze"], cwd=root, timeout=timeout)
        return _run(["dart", "analyze"], cwd=root, timeout=timeout)
    if (root / "go.mod").exists():
        return _run(["go", "build", "./..."], cwd=root, timeout=timeout)
    if (root / "package.json").exists():
        return _run(["npm", "run", "build"], cwd=root, timeout=timeout)
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return _run([str(root / "gradlew.bat") if os.name == "nt" else "./gradlew", "build"],
                    cwd=root, timeout=timeout)
    if (root / "pom.xml").exists():
        return _run(["mvn", "package", "-q"], cwd=root, timeout=timeout)
    if (root / "pyproject.toml").exists():
        return _run([sys.executable, "-I", "-m", "compileall", "-q", "."], cwd=root, timeout=timeout)

    return {"ok": False, "error": "no recognized build system found at %s" % root}


def build_clean(root=".", timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 30, 5, MAX_TIMEOUT)

    if (root / "Makefile").exists():
        return _run(["make", "clean"], cwd=root, timeout=timeout)
    if (root / "Cargo.toml").exists():
        return _run(["cargo", "clean"], cwd=root, timeout=timeout)
    if (root / "Package.swift").exists():
        return _run(["swift", "package", "clean"], cwd=root, timeout=timeout)
    if (root / "mix.exs").exists():
        return _run(["mix", "clean"], cwd=root, timeout=timeout)
    if (root / "WORKSPACE").exists() or (root / "MODULE.bazel").exists():
        return _run(["bazel", "clean"], cwd=root, timeout=timeout)
    if (root / "pubspec.yaml").exists() and shutil.which("flutter"):
        return _run(["flutter", "clean"], cwd=root, timeout=timeout)
    dotnet_targets = sorted(root.glob("*.sln")) or sorted(root.glob("*.csproj")) or sorted(root.glob("*.fsproj"))
    if dotnet_targets:
        return _run(["dotnet", "clean", dotnet_targets[0].name], cwd=root, timeout=timeout)
    if (root / "go.mod").exists():
        return _run(["go", "clean", "./..."], cwd=root, timeout=timeout)
    return {"ok": False, "error": "no recognized build system for clean"}


# ---------------------------------------------------------------------------
# Refactoring helpers
# ---------------------------------------------------------------------------

def _has_reparse_component(path, root):
    """True when any component strictly below ``root`` is a symlink or junction."""
    current = Path(path)
    root_key = os.path.normcase(str(Path(root)))
    while os.path.normcase(str(current)) != root_key and current.parent != current:
        if file_ops.is_reparse_point(current):
            return True
        current = current.parent
    return False


def _refactor_source_files(root, glob):
    """Yield ``(path, rel)`` for regular files the refactoring helpers inspect.

    Dot-directories are skipped only BELOW the root.  Testing the absolute
    path skipped every file whenever the root itself had a dot-prefixed
    ancestor (``~/.claude/worktrees/...``), so both helpers matched nothing.
    Reparse points (symlinks/junctions) below ``root`` are yielded before any
    ``is_file``/``stat`` call so callers refuse or skip them without following.
    """
    glob_text = str(glob or "").strip()
    if not glob_text or any(ch in glob_text for ch in ("\0", "\r", "\n")):
        return
    glob_path = Path(glob_text)
    if glob_path.is_absolute() or glob_text.startswith(("\\\\", "//")) or ".." in glob_path.parts:
        return
    for path in root.glob(glob_text):
        try:
            rel_path = path.relative_to(root)
        except ValueError:
            continue
        if any(part.startswith(".") for part in rel_path.parts):
            continue
        if _has_reparse_component(path, root):
            yield path, str(rel_path)
            continue
        try:
            if not path.is_file() or path.stat().st_size > 2_000_000:
                continue
        except OSError:
            continue
        yield path, str(rel_path)


def _protected_source(path, root):
    """The real location of a refactoring source file it may read, else None.

    Mirrors the file tools' read guard: reparse points, files resolving outside
    ``root``, credential stores and protected secret/control-plane paths are
    never read.
    """
    if _has_reparse_component(path, root):
        return None
    real = _secret_scan_adapter.confined_path(path, root)
    if real is None:
        return None
    if (
        file_ops.credential_read_component(path)
        or file_ops.credential_read_component(real)
        or file_ops.is_credential_store(path)
        or file_ops.is_credential_store(real)
    ):
        return None
    if file_ops._is_protected_read_path(path) or file_ops._is_protected_read_path(real):
        return None
    return real


def _is_protected_read_target(path):
    """True when ``path`` (the path of an opened handle) is a credential store or secret/control state."""
    return bool(
        file_ops.credential_read_component(path)
        or file_ops.is_credential_store(path)
        or file_ops._is_protected_read_path(path)
    )


_MAX_SOURCE_BYTES = 2_000_000
_MAX_DIFF_BYTES = 8_000_000


def _read_source(path, root):
    """Decode ``path`` through a validated open handle (see ``confined_read``).

    newline="" semantics: bytes are decoded as-is so each file's own line
    endings survive a rewrite. Raises ``PermissionError`` on a confinement
    refusal, ``UnicodeDecodeError``/``OSError`` otherwise.
    """
    data = confined_read.read_confined_bytes(
        root, path, max_bytes=_MAX_SOURCE_BYTES, is_protected=_is_protected_read_target)
    return data.decode("utf-8")


def _apply_staged_rewrites(staged, root):
    """Write every staged rewrite, or none of them.

    Each target's parent folder is opened first and held (``confined_read.PinnedDirectory``),
    so a parent swapped for a junction after validation cannot redirect the temp file or the
    replace outside ``root``. Each new content is first written to a temporary file beside
    its target; only when all are staged are they swapped in. If any step fails, staged
    files are removed and already-swapped targets are restored to their original content,
    so a failure never leaves a half-renamed tree.
    """
    temps = []
    applied = []
    with contextlib.ExitStack() as stack:
        pins = {}

        def pinned(path):
            key = os.path.normcase(os.path.abspath(str(path.parent)))
            if key not in pins:
                pins[key] = stack.enter_context(confined_read.PinnedDirectory(root, path.parent))
            return pins[key]

        try:
            for path, _original, new_content in staged:
                pin = pinned(path)
                temps.append((pin, pin.stage(path.name, new_content)))
            for (path, original, _new), (pin, temp) in zip(staged, temps, strict=True):
                pin.commit(temp, path.name)
                applied.append((path, original))
        except BaseException as error:
            for pin, temp in temps:
                pin.discard(temp)
            unrestored = []
            for path, original in reversed(applied):
                try:
                    pinned(path).rewrite(path.name, original)
                except OSError:
                    unrestored.append(path)
            if unrestored:
                raise OSError(
                    "rename rollback could not restore %d file(s)" % len(unrestored)
                ) from error
            raise


def _rename_refusal(error, dry_run, old_name, new_name):
    return {
        "ok": False,
        "error": error,
        "dry_run": dry_run,
        "old_name": old_name,
        "new_name": new_name,
        "files_changed": 0,
    }


def rename_symbol(root=".", old_name="", new_name="", glob="**/*.py", dry_run=True, timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not old_name or not new_name:
        return {"ok": False, "error": "both old_name and new_name are required"}

    files_changed = []
    preview = []
    staged = []
    pattern = re.compile(r'\b' + re.escape(old_name) + r'\b')

    for path, rel in _refactor_source_files(root, glob):
        # Enforce read guards before opening: skipping a protected file could
        # leave a half-renamed tree or reveal which symbols it holds.
        real = _protected_source(path, root)
        if real is None:
            if _has_reparse_component(path, root) or _secret_scan_adapter.confined_path(path, root) is None:
                return _rename_refusal(
                    "refusing to rename across %s: it is a link or lies outside the root; "
                    "narrow the glob to exclude it" % rel,
                    dry_run, old_name, new_name,
                )
            return _rename_refusal(
                "refusing to rename: the glob reaches a credential store or a protected "
                "secret/control-plane file; narrow the glob to exclude it",
                dry_run, old_name, new_name,
            )
        try:
            content = _read_source(path, root)
        except confined_read.ConfinedReadRefused as exc:
            return _rename_refusal(
                "refusing to rename: %s is not safely readable (%s); narrow the glob to exclude it" % (rel, exc),
                dry_run, old_name, new_name,
            )
        except (UnicodeDecodeError, OSError):
            continue
        matches = list(pattern.finditer(content))
        if not matches:
            continue
        new_content = pattern.sub(new_name, content)
        count = len(matches)
        files_changed.append({"file": rel, "replacements": count})
        for m in matches[:3]:
            line_no = content[:m.start()].count("\n") + 1
            line = content.splitlines()[line_no - 1].strip()
            preview.append({"file": rel, "line": line_no, "text": line})
        staged.append((real, content, new_content))

    if not dry_run and staged:
        refused = protected_write_scope.files_refusal("rename_symbol", root, [path for path, _old, _new in staged])
        if refused:
            return _rename_refusal(refused, dry_run, old_name, new_name)
        try:
            _apply_staged_rewrites(staged, root)
        except OSError as exc:
            return _rename_refusal("rename failed and was rolled back: %s" % exc, dry_run, old_name, new_name)

    return {
        "ok": True,
        "dry_run": dry_run,
        "old_name": old_name,
        "new_name": new_name,
        "files_changed": len(files_changed),
        "total_replacements": sum(f["replacements"] for f in files_changed),
        "details": files_changed,
        "preview": preview[:20],
    }


def extract_references(root=".", symbol="", glob="**/*.py", timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not symbol:
        return {"ok": False, "error": "symbol name is required"}

    pattern = re.compile(r'\b' + re.escape(symbol) + r'\b')
    refs = []
    for path, rel in _refactor_source_files(root, glob):
        real = _protected_source(path, root)
        if real is None:
            continue
        try:
            content = _read_source(path, root)
        except (UnicodeDecodeError, OSError):
            continue
        lines = content.splitlines()
        for i, line in enumerate(lines, 1):
            if pattern.search(line):
                refs.append({"file": rel, "line": i, "text": line.strip()})
                if len(refs) >= 200:
                    return {"ok": True, "symbol": symbol, "references": refs, "truncated": True}

    return {"ok": True, "symbol": symbol, "references": refs, "truncated": False}


# ---------------------------------------------------------------------------
# Diff / patch helpers
# ---------------------------------------------------------------------------


def _prepare_diff_side(root, requested, label):
    """Confine and read-guard one operand of ``diff_files`` before ``is_file``."""
    text = str(requested or "").strip()
    if not text:
        return None, None, {"ok": False, "error": "both left and right paths are required"}
    if any(ch in text for ch in ("\0", "\r", "\n")):
        return None, None, {"ok": False, "error": "left and right must resolve inside the root directory"}
    try:
        file_ops._reject_network_path(text)
    except PermissionError:
        return None, None, {"ok": False, "error": "left and right must resolve inside the root directory"}
    if file_ops._foreign_absolute(text):
        return None, None, {"ok": False, "error": "left and right must resolve inside the root directory"}
    candidate = Path(text)
    joined = Path(os.path.normpath(str(candidate if candidate.is_absolute() else root / candidate)))
    if joined != root and not file_ops._is_inside(joined, root):
        return None, None, {"ok": False, "error": "left and right must resolve inside the root directory"}
    if _has_reparse_component(joined, root):
        return None, None, {"ok": False, "error": "left and right must resolve inside the root directory"}
    # Judged on the caller's spelling: resolve() below expands a short name to the long one.
    if confined_read.short_name_component(root, joined) is not None:
        return None, None, {
            "ok": False,
            "error": "refusing to diff %s file %s: a component is shaped like a Windows 8.3 "
                     "short name (NAME~N.EXT); give the long name" % (label, requested),
        }
    try:
        resolved = joined.resolve()
        rel = resolved.relative_to(root)
    except (OSError, RuntimeError, ValueError):
        return None, None, {"ok": False, "error": "left and right must resolve inside the root directory"}
    if (
        file_ops.credential_read_component(joined)
        or file_ops.credential_read_component(resolved)
        or file_ops._is_protected_read_path(joined)
        or file_ops._is_protected_read_path(resolved)
    ):
        return None, None, {
            "ok": False,
            "error": "refusing to diff %s file %s: it is secret or control state" % (label, requested),
        }
    return resolved, rel, None


def diff_files(root=".", left="", right="", context=3, timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not left or not right:
        return {"ok": False, "error": "both left and right paths are required"}
    context = _bounded_int(context, 3, 0, 1000)
    timeout = _bounded_int(timeout, 10, 1, MAX_TIMEOUT)
    left_path, left_rel, err = _prepare_diff_side(root, left, "left")
    if err:
        return err
    right_path, right_rel, err = _prepare_diff_side(root, right, "right")
    if err:
        return err
    sides = []
    for label, requested, path in (("left", left, left_path), ("right", right, right_path)):
        try:
            sides.append(confined_read.read_confined_bytes(
                root, path, max_bytes=_MAX_DIFF_BYTES, is_protected=_is_protected_read_target))
        except confined_read.ConfinedReadRefused:
            return {
                "ok": False,
                "error": "refusing to diff %s file %s: it is a link, an alias, outside the root, "
                         "or secret or control state" % (label, requested),
            }
        except confined_read.ConfinedReadTooLarge:
            return {"ok": False, "error": "%s file is over the %d byte diff cap: %s" % (label, _MAX_DIFF_BYTES, requested)}
        except OSError:
            return {"ok": False, "error": "%s file not found: %s" % (label, requested)}
    # The diff is built from the bytes already read through the validated handles;
    # git is never handed the pathnames again.
    return _diff_bytes(sides[0], sides[1], left_rel.as_posix(), right_rel.as_posix(), context, timeout, root)


# Runs in a child interpreter (``-I``) that the parent kills at the timeout: difflib is
# quadratic on hostile inputs and cannot be interrupted in-process. stdin carries one JSON
# header line (byte lengths, context, names) followed by the left and right bytes.
_DIFF_CHILD = "\n".join((
    "import sys, json, difflib",
    "raw = sys.stdin.buffer",
    "head = json.loads(raw.readline())",
    "left = raw.read(head['l']).decode('utf-8', errors='replace').splitlines()",
    "right = raw.read(head['r']).decode('utf-8', errors='replace').splitlines()",
    "body = difflib.unified_diff(left, right, fromfile='a/' + head['a'], tofile='b/' + head['b'],"
    " n=head['n'], lineterm='')",
    "text = 'diff --git a/%s b/%s' % (head['a'], head['b']) + chr(10) + chr(10).join(body) + chr(10)",
    "sys.stdout.buffer.write(text.encode('utf-8', errors='replace'))",
    "",
))


def _diff_bytes(left_bytes, right_bytes, left_name, right_name, context, timeout=10, root="."):
    started = time.time()
    command = ["diff_files", left_name, right_name]
    if left_bytes == right_bytes:
        stdout = ""
    elif b"\0" in left_bytes[:8192] or b"\0" in right_bytes[:8192]:
        stdout = "Binary files a/%s and b/%s differ\n" % (left_name, right_name)
    else:
        header = json.dumps({
            "l": len(left_bytes), "r": len(right_bytes), "n": context, "a": left_name, "b": right_name,
        }).encode("utf-8") + b"\n"
        child = _run(
            [sys.executable, "-I", "-c", _DIFF_CHILD], cwd=root, timeout=timeout,
            stdin_bytes=header + left_bytes + right_bytes,
        )
        if child.get("timed_out"):
            return {
                "ok": False, "returncode": -1, "timed_out": True,
                "elapsed_ms": int((time.time() - started) * 1000), "stdout": "",
                "stderr": "diff_files exceeded the %d second timeout" % timeout,
                "error": "diff exceeded the %d second timeout; compare smaller files" % timeout,
                "command": command,
            }
        if child.get("returncode") != 0:
            return {
                "ok": False, "returncode": child.get("returncode", -1), "timed_out": False,
                "elapsed_ms": int((time.time() - started) * 1000), "stdout": "",
                "stderr": child.get("stderr", ""), "error": "diff failed", "command": command,
            }
        stdout = child.get("stdout", "")
    return {
        "ok": True,
        "returncode": 1 if stdout else 0,
        "timed_out": False,
        "elapsed_ms": int((time.time() - started) * 1000),
        "stdout": _trim(stdout),
        "stderr": "",
        "command": command,
    }


def apply_patch(root=".", patch_text="", check_only=False, timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not patch_text:
        return {"ok": False, "error": "patch_text is required"}
    if not check_only:
        refused = _live_refusal("apply_patch", root)
        if refused:
            return refused
        # Every path the patch writes, in any repository, gets the file tools' guard.
        refused = _patch_refusal("apply_patch", root, patch_text, timeout=timeout)
        if refused:
            return refused
    cmd = [_git(), "apply"]
    if check_only:
        cmd.append("--check")
    cmd.append("-")
    return _run(cmd, cwd=root, timeout=timeout, stdin_bytes=patch_text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Security scanning
# ---------------------------------------------------------------------------

_REDACTED_CREDENTIAL = "[REDACTED CREDENTIAL]"
_HARNESS_FINDING_LABELS = {
    "private_key": "Private key",
    "aws_access_key": "AWS credential",
    "github_token": "GitHub PAT",
    "api_token": "API key",
    "bearer_token": "Bearer token",
    "url_credential": "URL credential",
    "jwt": "JWT",
    "credential_assignment": "Secret/password",
}


def secret_scan(root=".", timeout=30, extra_roots=""):
    resolved = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 30, 5, MAX_TIMEOUT)
    # Delegate the walk, symlink/junction confinement, credential-store skip
    # and CredentialScanner matching to the canonical adapter so the live MCP
    # tool shares the exact same security guarantees.
    result = _secret_scan_adapter._scan_resolved(resolved, timeout=timeout)
    findings = [
        {
            "file": item["file"],
            "line": item["line"],
            "type": _HARNESS_FINDING_LABELS.get(item["type"], item["type"]),
            "match": _REDACTED_CREDENTIAL,
        }
        for item in result["findings"]
    ]
    return {
        "ok": True,
        "findings": findings,
        "files_scanned": result["files_scanned"],
        "truncated": result["truncated"],
        "timed_out": result["timed_out"],
        "timeout": timeout,
        "credential_files_skipped": result["credential_files_skipped"],
        "links_skipped": result["links_skipped"],
    }
