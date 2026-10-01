"""Small, bounded, local syntax/checker used after agent file edits.

The checker deliberately does not install or discover tools from another
virtual environment.  It reports source locations in a stable, compact form
so an agent can repair a file in the next step.
"""
from __future__ import annotations

import ast
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

MAX_BYTES = 1_000_000
MAX_SECONDS = 10.0
_DEADLINE: ContextVar[float | None] = ContextVar("file_check_deadline", default=None)
CHECKABLE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".json", ".toml", ".yaml", ".yml", ".c", ".cc", ".cpp", ".cxx", ".h", ".hpp", ".ts"}


@dataclass(frozen=True)
class CheckIssue:
    line: int
    column: int
    checker: str
    code: str
    message: str


@contextmanager
def check_budget():
    """Share one ten-second budget across automatic checks of a patch."""
    deadline = min(time.monotonic() + MAX_SECONDS, _DEADLINE.get() or float("inf"))
    token = _DEADLINE.set(deadline)
    try:
        yield deadline
    finally:
        _DEADLINE.reset(token)


def is_checkable(path: str | os.PathLike[str]) -> bool:
    return Path(path).suffix.lower() in CHECKABLE_SUFFIXES


def _issue(line: int, column: int, checker: str, code: str, message: str) -> CheckIssue:
    return CheckIssue(max(1, int(line or 1)), max(1, int(column or 1)), checker, code, str(message).strip())


def _bounded(text: str) -> str:
    return text if len(text) <= 2000 else text[:1997].rstrip() + "..."


def _syntax_python(path: Path, source: str) -> list[CheckIssue]:
    try:
        tree = ast.parse(source, filename=str(path))
        compile(tree, str(path), "exec")
        return []
    except (SyntaxError, ValueError, RecursionError) as exc:
        return [_issue(getattr(exc, "lineno", 1), getattr(exc, "offset", 1), "python", "SyntaxError", getattr(exc, "msg", str(exc)))]


def _ruff_path() -> str | None:
    configured = os.environ.get("SONDER_LINTER_PATH", "").strip()
    if configured:
        candidate = Path(shutil.which(configured) or configured)
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return shutil.which("ruff")


def _external(command: list[str], checker: str, deadline: float) -> list[CheckIssue]:
    remaining = min(MAX_SECONDS, deadline - time.monotonic())
    if remaining <= 0:
        return [_issue(1, 1, checker, "timeout", "check budget exhausted")]
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=remaining,
                                   check=False, shell=False, encoding="utf-8", errors="replace")
    except subprocess.TimeoutExpired:
        return [_issue(1, 1, checker, "timeout", "checker timed out")]
    except OSError as exc:
        return [_issue(1, 1, checker, "error", "checker failed: %s" % exc)]
    if completed.returncode == 0:
        return []
    issues: list[CheckIssue] = []
    output = completed.stdout + completed.stderr
    if checker == "node":
        location = re.search(r"^.+?:(\d+)(?::(\d+))?\s*$", output, re.M)
        error = re.search(r"^(SyntaxError|Error):\s*(.*)$", output, re.M)
        if error:
            return [_issue(int(location[1]) if location else 1, int(location[2] or 1) if location else 1,
                           checker, error[1], error[2])]
    for text in output.splitlines():
        # ruff: path:line:col: CODE message; node/tsc have related forms.
        if checker == "tsc":
            tsc_match = re.search(r"\((\d+),(\d+)\):\s*(?:error\s+)?(TS\d+)?\s*:?\s*(.*)$", text)
            if tsc_match:
                line, col, code, message = tsc_match.groups()
                issues.append(_issue(int(line), int(col), checker, code or "error", message))
                continue
        if checker == "pyflakes":
            match = re.search(r":(\d+)(?::(\d+))?:\s*(.*)$", text)
            if match:
                issues.append(_issue(int(match[1]), int(match[2] or 1), checker, "warning", match[3]))
                continue
        match = re.search(r":(\d+):(\d+)(?::\s*|\s+)(?:(\w+)\s+)?(.*)$", text)
        if match:
            line, col, code, message = match.groups()
            issues.append(_issue(int(line), int(col), checker, code or "error", message))
            continue
        if text.strip():
            issues.append(_issue(1, 1, checker, "error", text))
    if not issues:
        issues.append(_issue(1, 1, checker, "error", "checker failed with exit code %s" % completed.returncode))
    return issues


def _parse_json(path: Path, source: str) -> list[CheckIssue]:
    try:
        json.loads(source)
    except json.JSONDecodeError as exc:
        return [_issue(exc.lineno, exc.colno, "json", "JSONDecodeError", exc.msg)]
    return []


def _parse_toml(path: Path, source: str) -> list[CheckIssue]:
    try:
        tomllib.loads(source)
    except tomllib.TOMLDecodeError as exc:
        location = re.search(r"at line (\d+), column (\d+)", str(exc))
        return [_issue(int(location[1]) if location else 1, int(location[2]) if location else 1,
                       "toml", "TOMLDecodeError", str(exc))]
    return []


def check_file(path: str | os.PathLike[str], max_items: int = 30, *, project_root: str | os.PathLike[str] | None = None) -> str:
    """Check one source file and return the bounded agent-facing report."""
    started = time.monotonic()
    deadline = min(started + MAX_SECONDS, _DEADLINE.get() or float("inf"))
    requested = str(path)
    limit = max(1, min(100, int(max_items)))
    try:
        from .filesystem.file_ops import resolve_repository_read_path
        if project_root is None:
            # Reuse the guarded repository-read resolver.  It consults the
            # operator's configured roots and rejects credentials/control data.
            target = resolve_repository_read_path(requested)
        else:
            root = Path(project_root).expanduser().resolve()
            candidate = Path(requested).expanduser()
            target = (root / candidate if not candidate.is_absolute() else candidate).resolve()
            target.relative_to(root)
            # Keep the ordinary credential/control-plane guard in force even
            # when the host supplies a project root. ``extra_roots`` here is
            # host-derived, never model-supplied.
            target = resolve_repository_read_path(str(target), extra_roots=str(root))
            requested = target.relative_to(root).as_posix()
    except (OSError, ValueError, PermissionError) as exc:
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check scope {exc}")
    if not target.is_file():
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check NotFound file does not exist")
    try:
        size = target.stat().st_size
    except OSError as exc:
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check OSError {exc}")
    if size > MAX_BYTES:
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check too_large file exceeds 1 MB")
    try:
        with target.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
    except OSError as exc:
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check OSError {exc}")
    if len(raw) > MAX_BYTES:
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check too_large file exceeds 1 MB")
    if b"\0" in raw:
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check binary binary file refused")
    try:
        source = raw.decode("utf-8")
    except UnicodeDecodeError:
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check binary non-UTF-8 file refused")
    suffix = target.suffix.lower()
    if time.monotonic() >= deadline:
        return _bounded(f"file_check {requested}: 1 issue(s)\n1:1: file_check timeout check budget exhausted")
    issues: list[CheckIssue] = []
    if suffix == ".py":
        issues.extend(_syntax_python(target, source))
        if not issues:
            if importlib.util.find_spec("pyflakes") is None:
                ruff = _ruff_path()
                if ruff:
                    issues.extend(_external([ruff, "check", "--isolated", "--no-fix", "--no-cache",
                                             "--output-format", "concise", str(target)], "ruff", deadline))
                else:
                    issues.append(_issue(1, 1, "file_check", "note", "no linter available"))
            else:
                issues.extend(_external([sys.executable, "-I", "-m", "pyflakes", str(target)], "pyflakes", deadline))
    elif suffix in {".js", ".mjs", ".cjs"}:
        node = shutil.which("node")
        if node:
            issues.extend(_external([node, "--check", str(target)], "node", deadline))
        else:
            issues.append(_issue(1, 1, "file_check", "note", "node not available"))
    elif suffix == ".json":
        issues.extend(_parse_json(target, source))
    elif suffix == ".toml":
        issues.extend(_parse_toml(target, source))
    elif suffix in {".yaml", ".yml"}:
        issues.append(_issue(1, 1, "file_check", "note", "yaml checker skipped"))
    elif suffix in {".c", ".cc", ".cpp", ".cxx", ".h", ".hpp"}:
        issues.append(_issue(1, 1, "file_check", "note", "C/C++ checker skipped; use build.ps1"))
    elif suffix == ".ts":
        tsc = shutil.which("tsc")
        if tsc:
            issues.extend(_external([tsc, "--noEmit", str(target)], "tsc", deadline))
        else:
            issues.append(_issue(1, 1, "file_check", "note", "tsc not available"))
    else:
        issues.append(_issue(1, 1, "file_check", "note", "checker skipped for file type"))
    issues.sort(key=lambda item: (item.line, item.column, item.checker, item.code, item.message))
    count = len(issues)
    if not issues:
        return _bounded(f"file_check {requested}: none")
    if all(item.code == "note" for item in issues):
        return _bounded(f"file_check {requested}: none\n" + "\n".join(
            "note: " + item.message for item in issues
        ))
    lines = [f"file_check {requested}: {count} issue(s)"]
    for item in issues[:limit]:
        lines.append(f"{item.line}:{item.column}: {item.checker} {item.code} {item.message}")
    output = "\n".join(lines)
    if len(output) > 2000:
        output = output[:1997].rstrip() + "..."
    return output


def file_check(path: str, max_items: int = 30) -> str:
    return check_file(path, max_items)


__all__ = ["CHECKABLE_SUFFIXES", "MAX_BYTES", "MAX_SECONDS", "check_file", "file_check", "is_checkable"]
