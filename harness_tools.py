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
import shutil
import subprocess
import sys
import time
from pathlib import Path

import file_ops
import sonder_paths
import unsafe_lab

MAX_OUTPUT = 256_000
MAX_TIMEOUT = 120
DEFAULT_TIMEOUT = 30


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
    ``secret_scan`` prints the credentials it matches. Until now this function
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

        unauthorized MISSING  -> ValueError: not a directory: C:\\Users\\natew\\__definitely_not_here__
        unauthorized EXISTING -> PermissionError: root is outside every authorized root: C:\\Windows\\System32\\drivers

    Every server wrapper does ``return "ERROR: %s" % exc``, so a confined agent
    could probe the existence of any path on the host and read the resolved
    location back -- the same class of leak the ``diff_files`` absolute-path
    fix closed in this module. An authorized root may still report "not a
    directory" with its path: the caller is entitled to that one.
    """
    p = Path(root or ".").resolve()
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
    root = Path(root)
    candidate = Path(text)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if resolved != root and not file_ops._is_inside(resolved, root):
        raise ValueError(
            "path is outside the root it was given: %s is not inside %s"
            % (text, root)
        )
    try:
        return str(resolved.relative_to(root)) or "."
    except ValueError:
        return str(resolved)


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
    env = os.environ.copy()
    env.pop("CC", None)
    env.pop("CXX", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _run(cmd, *, cwd, timeout=DEFAULT_TIMEOUT, stdin_bytes=b"", env=None):
    started = time.time()
    timed_out = False
    try:
        proc = subprocess.run(
            cmd, cwd=str(cwd), timeout=timeout,
            capture_output=True, input=stdin_bytes,
            env=env or _child_env(),
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return {
            "ok": False, "returncode": -1, "timed_out": True,
            "elapsed_ms": int((time.time() - started) * 1000),
            "stdout": _trim(exc.stdout.decode("utf-8", errors="replace") if exc.stdout else ""),
            "stderr": _trim(exc.stderr.decode("utf-8", errors="replace") if exc.stderr else ""),
            "command": [str(c) for c in cmd],
        }
    except FileNotFoundError:
        return {
            "ok": False, "returncode": -1, "timed_out": False,
            "elapsed_ms": int((time.time() - started) * 1000),
            "stdout": "", "stderr": "command not found: %s" % cmd[0],
            "command": [str(c) for c in cmd],
        }
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "timed_out": False,
        "elapsed_ms": int((time.time() - started) * 1000),
        "stdout": _trim(proc.stdout.decode("utf-8", errors="replace")),
        "stderr": _trim(proc.stderr.decode("utf-8", errors="replace")),
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


def _run_git(root, args, *, timeout=10):
    env = _child_env()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return _run([_git()] + args, cwd=root, timeout=timeout, env=env)


# ---------------------------------------------------------------------------
# Test runner
# ---------------------------------------------------------------------------

_TEST_FRAMEWORKS = {
    "pytest": {"cmd": [sys.executable, "-m", "pytest"], "marker_files": ["pytest.ini", "pyproject.toml", "setup.cfg", "conftest.py"]},
    "unittest": {"cmd": [sys.executable, "-m", "unittest", "discover"], "marker_files": []},
    "jest": {"cmd": ["npx", "jest"], "marker_files": ["jest.config.js", "jest.config.ts", "jest.config.mjs"]},
    "vitest": {"cmd": ["npx", "vitest", "run"], "marker_files": ["vitest.config.ts", "vitest.config.js", "vitest.config.mts"]},
    "mocha": {"cmd": ["npx", "mocha"], "marker_files": [".mocharc.yml", ".mocharc.json", ".mocharc.js"]},
    "cargo": {"cmd": ["cargo", "test"], "marker_files": ["Cargo.toml"]},
    "go": {"cmd": ["go", "test", "./..."], "marker_files": ["go.mod"]},
    "dotnet": {"cmd": ["dotnet", "test"], "marker_files": ["*.csproj", "*.sln"]},
}


def _detect_test_framework(root):
    root = Path(root)
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
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "--no-header"],
            cwd=root, timeout=30,
        )
        if result["ok"]:
            lines = result["stdout"].strip().splitlines()
            tests = [l for l in lines if "::" in l]
            info["test_files"] = sorted(set(t.split("::")[0] for t in tests))
            info["test_count"] = len(tests)
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
    coverage=False, timeout=120, extra_args_json="[]",
    extra_roots="",
):
    root = _resolve_root(root, extra_roots)
    # Confine the argv-appended path too; see _resolve_target_path.
    path = _resolve_target_path(root, path)
    timeout = _bounded_int(timeout, 120, 5, MAX_TIMEOUT)
    if framework == "auto":
        framework = _detect_test_framework(root)

    base = list(_TEST_FRAMEWORKS.get(framework, {}).get("cmd", []))
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

    try:
        extra = json.loads(extra_args_json)
        if isinstance(extra, list):
            cmd.extend(str(a) for a in extra)
    except (json.JSONDecodeError, TypeError):
        pass

    result = _run(cmd, cwd=root, timeout=timeout)
    result["framework"] = framework
    return result


# ---------------------------------------------------------------------------
# Lint / Format / Type check
# ---------------------------------------------------------------------------

_LINT_TOOLS = {
    "ruff": {"check": ["ruff", "check"], "fix": ["ruff", "check", "--fix"]},
    "flake8": {"check": ["flake8"], "fix": None},
    "pylint": {"check": ["pylint"], "fix": None},
    "eslint": {"check": ["npx", "eslint"], "fix": ["npx", "eslint", "--fix"]},
    "clippy": {"check": ["cargo", "clippy"], "fix": ["cargo", "clippy", "--fix", "--allow-dirty"]},
}

_FORMATTERS = {
    "ruff": ["ruff", "format"],
    "black": ["black"],
    "prettier": ["npx", "prettier", "--write"],
    "rustfmt": ["cargo", "fmt"],
    "gofmt": ["gofmt", "-w"],
    "clang-format": ["clang-format", "-i"],
}

_TYPECHECKERS = {
    "mypy": [sys.executable, "-m", "mypy"],
    "pyright": ["npx", "pyright"],
    "tsc": ["npx", "tsc", "--noEmit"],
}


def _detect_linter(root):
    root = Path(root)
    if shutil.which("ruff") and (root / "pyproject.toml").exists():
        return "ruff"
    if (root / ".eslintrc.js").exists() or (root / ".eslintrc.json").exists() or (root / "eslint.config.js").exists():
        return "eslint"
    if (root / "Cargo.toml").exists():
        return "clippy"
    if shutil.which("ruff"):
        return "ruff"
    if shutil.which("flake8"):
        return "flake8"
    return "ruff"


def _detect_formatter(root):
    root = Path(root)
    if shutil.which("ruff") and (root / "pyproject.toml").exists():
        return "ruff"
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
        cmd = list(info["fix"])
    else:
        cmd = list(info["check"])
    if path:
        cmd.append(path)
    elif tool in ("ruff", "flake8", "pylint"):
        cmd.append(".")
    result = _run(cmd, cwd=root, timeout=timeout)
    result["tool"] = tool
    result["mode"] = "fix" if fix else "check"
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
    cmd = list(cmd_template)
    if check_only:
        if tool in ("ruff", "black"):
            cmd.append("--check")
        elif tool == "prettier":
            cmd[-1] = "--check"
        elif tool == "rustfmt":
            cmd.append("--check")
    if path:
        cmd.append(path)
    elif tool in ("ruff", "black"):
        cmd.append(".")
    result = _run(cmd, cwd=root, timeout=timeout)
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
    if (root / "requirements.txt").exists() or (root / "pyproject.toml").exists():
        return "pip"
    if (root / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (root / "yarn.lock").exists():
        return "yarn"
    if (root / "package.json").exists():
        return "npm"
    if (root / "Gemfile").exists():
        return "bundler"
    return "pip"


def dependency_add(root=".", packages_json="[]", dev=False, timeout=60, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 60, 5, MAX_TIMEOUT)
    try:
        packages = json.loads(packages_json)
        if not isinstance(packages, list) or not packages:
            return {"ok": False, "error": "packages_json must be a non-empty JSON array of package names"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid JSON in packages_json"}

    mgr = _detect_package_manager(root)
    if mgr == "pip":
        cmd = [sys.executable, "-m", "pip", "install"] + packages
    elif mgr == "npm":
        cmd = ["npm", "install"] + (["--save-dev"] if dev else []) + packages
    elif mgr == "pnpm":
        cmd = ["pnpm", "add"] + (["-D"] if dev else []) + packages
    elif mgr == "yarn":
        cmd = ["yarn", "add"] + (["--dev"] if dev else []) + packages
    elif mgr == "cargo":
        cmd = ["cargo", "add"] + packages
    elif mgr == "go":
        cmd = ["go", "get"] + packages
    else:
        return {"ok": False, "error": "unsupported package manager: %s" % mgr}

    result = _run(cmd, cwd=root, timeout=timeout)
    result["manager"] = mgr
    result["packages"] = packages
    return result


def dependency_remove(root=".", packages_json="[]", timeout=60, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 60, 5, MAX_TIMEOUT)
    try:
        packages = json.loads(packages_json)
        if not isinstance(packages, list) or not packages:
            return {"ok": False, "error": "packages_json must be a non-empty JSON array"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid JSON"}

    mgr = _detect_package_manager(root)
    if mgr == "pip":
        cmd = [sys.executable, "-m", "pip", "uninstall", "-y"] + packages
    elif mgr == "npm":
        cmd = ["npm", "uninstall"] + packages
    elif mgr == "pnpm":
        cmd = ["pnpm", "remove"] + packages
    elif mgr == "yarn":
        cmd = ["yarn", "remove"] + packages
    elif mgr == "cargo":
        cmd = ["cargo", "remove"] + packages
    else:
        return {"ok": False, "error": "unsupported for remove: %s" % mgr}

    result = _run(cmd, cwd=root, timeout=timeout)
    result["manager"] = mgr
    result["packages"] = packages
    return result


def dependency_update(root=".", packages_json="[]", timeout=120, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 120, 5, MAX_TIMEOUT)
    try:
        packages = json.loads(packages_json)
        if not isinstance(packages, list):
            packages = []
    except json.JSONDecodeError:
        packages = []

    mgr = _detect_package_manager(root)
    if mgr == "pip":
        if packages:
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade"] + packages
        else:
            cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "-r", "requirements.txt"]
    elif mgr == "npm":
        cmd = ["npm", "update"] + packages
    elif mgr == "pnpm":
        cmd = ["pnpm", "update"] + packages
    elif mgr == "yarn":
        cmd = ["yarn", "upgrade"] + packages
    elif mgr == "cargo":
        cmd = ["cargo", "update"] + (["--package"] + packages if packages else [])
    elif mgr == "go":
        cmd = ["go", "get", "-u"] + (packages or ["./..."])
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
        cmd = [sys.executable, "-m", "pip", "audit"] if shutil.which("pip-audit") else [sys.executable, "-m", "pip", "check"]
        result = _run(cmd, cwd=root, timeout=timeout)
    elif mgr in ("npm", "pnpm", "yarn"):
        cmd = ["npm", "audit", "--json"] if mgr == "npm" else [mgr, "audit"]
        result = _run(cmd, cwd=root, timeout=timeout)
    elif mgr == "cargo":
        if shutil.which("cargo-audit"):
            result = _run(["cargo", "audit"], cwd=root, timeout=timeout)
        else:
            result = {"ok": False, "error": "cargo-audit not installed; run: cargo install cargo-audit"}
    else:
        result = {"ok": False, "error": "no audit support for %s" % mgr}
    result["manager"] = mgr
    return result


# ---------------------------------------------------------------------------
# Git mutations
# ---------------------------------------------------------------------------

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


def git_branch(root=".", name="", checkout=True, base="", timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not name:
        return {"ok": False, "error": "branch name is required"}
    if checkout:
        cmd = ["checkout", "-b", name]
        if base:
            cmd.append(base)
    else:
        cmd = ["branch", name]
        if base:
            cmd.append(base)
    return _run_git(root, cmd, timeout=timeout)


def git_checkout(root=".", ref="", timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not ref:
        return {"ok": False, "error": "ref is required (branch name, tag, or commit)"}
    return _run_git(root, ["checkout", ref], timeout=timeout)


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
    return _run_git(root, cmd, timeout=timeout)


def git_tag(root=".", name="", message="", delete=False, timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not name:
        return {"ok": False, "error": "tag name is required"}
    if delete:
        cmd = ["tag", "-d", name]
    elif message:
        cmd = ["tag", "-a", name, "-m", message]
    else:
        cmd = ["tag", name]
    return _run_git(root, cmd, timeout=timeout)


def git_merge(root=".", branch="", no_ff=True, message="", timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not branch:
        return {"ok": False, "error": "branch name is required"}
    cmd = ["merge"]
    if no_ff:
        cmd.append("--no-ff")
    if message:
        cmd.extend(["-m", message])
    cmd.append(branch)
    return _run_git(root, cmd, timeout=timeout)


def git_cherry_pick(root=".", commits_json="[]", timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    try:
        commits = json.loads(commits_json)
        if not isinstance(commits, list) or not commits:
            return {"ok": False, "error": "commits_json must be a non-empty JSON array of commit SHAs"}
    except json.JSONDecodeError:
        return {"ok": False, "error": "invalid JSON"}
    return _run_git(root, ["cherry-pick"] + commits, timeout=timeout)


# ---------------------------------------------------------------------------
# Build tools
# ---------------------------------------------------------------------------

def build_run(root=".", command="", timeout=120, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 120, 5, MAX_TIMEOUT)

    if command:
        # `echo` and many build wrappers are shell builtins on Windows.  This
        # tool intentionally accepts a command string, so dispatch it through
        # cmd there rather than trying to exec a nonexistent echo.exe.
        if os.name == "nt":
            return _run(["cmd", "/d", "/s", "/c", command], cwd=root, timeout=timeout)
        parts = command.split()
        return _run(parts, cwd=root, timeout=timeout)

    if (root / "Makefile").exists():
        return _run(["make"], cwd=root, timeout=timeout)
    if (root / "Cargo.toml").exists():
        return _run(["cargo", "build"], cwd=root, timeout=timeout)
    if (root / "CMakeLists.txt").exists():
        return _run(["cmake", "--build", "build"], cwd=root, timeout=timeout)
    if (root / "go.mod").exists():
        return _run(["go", "build", "./..."], cwd=root, timeout=timeout)
    if (root / "package.json").exists():
        return _run(["npm", "run", "build"], cwd=root, timeout=timeout)
    if (root / "build.gradle").exists() or (root / "build.gradle.kts").exists():
        return _run(["./gradlew" if os.name != "nt" else "gradlew.bat", "build"], cwd=root, timeout=timeout)
    if (root / "pom.xml").exists():
        return _run(["mvn", "package", "-q"], cwd=root, timeout=timeout)

    return {"ok": False, "error": "no recognized build system found at %s" % root}


def build_clean(root=".", timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 30, 5, MAX_TIMEOUT)

    if (root / "Makefile").exists():
        return _run(["make", "clean"], cwd=root, timeout=timeout)
    if (root / "Cargo.toml").exists():
        return _run(["cargo", "clean"], cwd=root, timeout=timeout)
    if (root / "go.mod").exists():
        return _run(["go", "clean", "./..."], cwd=root, timeout=timeout)
    return {"ok": False, "error": "no recognized build system for clean"}


# ---------------------------------------------------------------------------
# Refactoring helpers
# ---------------------------------------------------------------------------

def rename_symbol(root=".", old_name="", new_name="", glob="**/*.py", dry_run=True, timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not old_name or not new_name:
        return {"ok": False, "error": "both old_name and new_name are required"}

    files_changed = []
    preview = []
    pattern = re.compile(r'\b' + re.escape(old_name) + r'\b')

    for path in root.glob(glob):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        rel = str(path.relative_to(root))
        if any(part.startswith(".") for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
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

        if not dry_run:
            path.write_text(new_content, encoding="utf-8")

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
    for path in root.glob(glob):
        if not path.is_file() or path.stat().st_size > 2_000_000:
            continue
        rel = str(path.relative_to(root))
        if any(part.startswith(".") for part in path.parts):
            continue
        try:
            content = path.read_text(encoding="utf-8")
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

def diff_files(root=".", left="", right="", context=3, timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not left or not right:
        return {"ok": False, "error": "both left and right paths are required"}
    left_path = (root / left).resolve()
    right_path = (root / right).resolve()
    if not left_path.is_file():
        return {"ok": False, "error": "left file not found: %s" % left}
    if not right_path.is_file():
        return {"ok": False, "error": "right file not found: %s" % right}
    # Pass the paths RELATIVE to root. `git diff --no-index` echoes whatever it
    # is given into the `diff --git a/... b/...` header, so absolute arguments
    # printed the operator's full host path -- where the project lives on disk,
    # and the account name in it -- into every diff a confined agent read back.
    # Resolving relative to the root also closes the hole this exposed: `left`
    # and `right` were joined to the root and never checked, so `../..` walked
    # straight out of it on any caller that does not go through the agent
    # surface's own scope check.
    try:
        left_rel = left_path.relative_to(root)
        right_rel = right_path.relative_to(root)
    except ValueError:
        return {
            "ok": False,
            "error": "left and right must resolve inside the root directory",
        }

    result = _run(
        [_git(), "diff", "--no-index", "-U%d" % context, "--", str(left_rel), str(right_rel)],
        cwd=root, timeout=timeout,
    )
    result["ok"] = True
    return result


def apply_patch(root=".", patch_text="", check_only=False, timeout=10, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    if not patch_text:
        return {"ok": False, "error": "patch_text is required"}
    cmd = [_git(), "apply"]
    if check_only:
        cmd.append("--check")
    cmd.append("-")
    return _run(cmd, cwd=root, timeout=timeout, stdin_bytes=patch_text.encode("utf-8"))


# ---------------------------------------------------------------------------
# Security scanning
# ---------------------------------------------------------------------------

def secret_scan(root=".", timeout=30, extra_roots=""):
    root = _resolve_root(root, extra_roots)
    timeout = _bounded_int(timeout, 30, 5, MAX_TIMEOUT)

    secret_patterns = [
        (r'(?i)(api[_-]?key|apikey)\s*[:=]\s*["\']?[a-z0-9]{20,}', "API key"),
        (r'(?i)(secret|password|passwd|pwd)\s*[:=]\s*["\'][^"\']{8,}', "Secret/password"),
        (r'(?i)(aws_access_key_id|aws_secret_access_key)\s*[:=]\s*["\']?[A-Z0-9]{16,}', "AWS credential"),
        (r'ghp_[a-zA-Z0-9]{36}', "GitHub PAT"),
        (r'sk-[a-zA-Z0-9]{32,}', "OpenAI/Stripe key"),
        (r'-----BEGIN (RSA |EC |DSA )?PRIVATE KEY-----', "Private key"),
        (r'(?i)bearer\s+[a-z0-9._\-]{20,}', "Bearer token"),
    ]

    findings = []
    scanned = 0
    for path in root.rglob("*"):
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        rel = str(path.relative_to(root))
        if any(part.startswith(".") and part != "." for part in Path(rel).parts):
            if not rel.startswith(".env"):
                continue
        if path.suffix.lower() in (".pyc", ".pyo", ".exe", ".dll", ".so", ".png", ".jpg", ".gif", ".ico", ".woff", ".woff2", ".ttf", ".zip", ".gz", ".tar", ".jar"):
            continue
        scanned += 1
        try:
            content = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern_str, label in secret_patterns:
            for m in re.finditer(pattern_str, content):
                line_no = content[:m.start()].count("\n") + 1
                findings.append({
                    "file": rel, "line": line_no, "type": label,
                    "match": m.group()[:40] + ("..." if len(m.group()) > 40 else ""),
                })
                if len(findings) >= 100:
                    return {"ok": True, "findings": findings, "files_scanned": scanned, "truncated": True}

    return {"ok": True, "findings": findings, "files_scanned": scanned, "truncated": False}
