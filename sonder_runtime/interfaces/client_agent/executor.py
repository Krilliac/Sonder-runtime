"""Workspace-contained client tool executor with client-local PermissionMode."""
from __future__ import annotations

import asyncio
import fnmatch
import os
import shlex
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MAX_OUTPUT_CHARS = 65_536
MAX_ERROR_CHARS = 4_096
_MAX_FILE_READ_CHARS = 65_536
_MAX_SEARCH_MATCHES = 100
_MAX_FIND_MATCHES = 200
_MAX_TREE_ENTRIES = 200

CLIENT_AGENT_MODES: tuple[str, ...] = (
    "plan",
    "manual",
    "acceptEdits",
    "auto",
    "bypass",
)

_READ_ONLY_TOOLS: frozenset[str] = frozenset({
    "file_read",
    "file_find",
    "directory_tree",
    "text_search",
    "git_status",
    "git_diff",
    "git_log",
    "git_branch",
})

_WRITE_TOOLS: frozenset[str] = frozenset({
    "file_write",
    "file_edit",
    "file_delete",
    "directory_create",
})

_EXECUTE_TOOLS: frozenset[str] = frozenset({
    "run_code",
    "run_program",
    "test_run",
})

CLIENT_DELEGATABLE_TOOLS: frozenset[str] = _READ_ONLY_TOOLS | _WRITE_TOOLS | _EXECUTE_TOOLS


def _bound_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 16)] + "\n...[truncated]"


def tool_risk_of(tool: str) -> str:
    if tool in _READ_ONLY_TOOLS:
        return "read_only"
    if tool in _WRITE_TOOLS:
        return "write"
    return "execute"


def mode_allows_without_prompt(mode: str, risk: str) -> bool:
    """Return whether client-local ``mode`` allows ``risk`` without explicit approval."""
    if mode == "bypass":
        return True
    if mode == "auto":
        return risk in {"read_only", "write", "execute"}
    if mode == "acceptEdits":
        return risk in {"read_only", "write"}
    return risk == "read_only"


@dataclass(frozen=True)
class ClientToolExecutionResult:
    ok: bool
    output: str = ""
    error: str = ""
    denied: bool = False
    needs_approval: bool = False
    mode: str = "manual"
    risk: str = "execute"

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "output": _bound_text(self.output, MAX_OUTPUT_CHARS),
            "error": _bound_text(self.error, MAX_ERROR_CHARS),
            "denied": self.denied,
            "needs_approval": self.needs_approval,
            "mode": self.mode,
            "risk": self.risk,
        }


class ClientAgentPolicy:
    """Mutable client-local mode & workspace policy owned by the local PC user."""

    def __init__(
        self,
        *,
        workspace_root: Path | str | None = None,
        mode: str = "manual",
    ) -> None:
        resolved_root = Path(workspace_root) if workspace_root is not None else Path.cwd()
        self.workspace_root: Path = resolved_root.resolve()
        if mode not in CLIENT_AGENT_MODES:
            raise ValueError(f"unknown client permission mode: {mode!r}")
        self.mode: str = mode

    def set_mode(self, mode: str, *, confirm_bypass: bool = False) -> str:
        clean = mode.strip()
        if clean not in CLIENT_AGENT_MODES:
            raise ValueError(f"unknown mode '{clean}'; valid modes: {', '.join(CLIENT_AGENT_MODES)}")
        if clean == "bypass" and not confirm_bypass:
            raise PermissionError("bypass mode requires explicit confirmation (confirm_bypass: true)")
        self.mode = clean
        return self.mode


def _resolve_path(raw: object, root: Path, *, allow_escape: bool) -> Path:
    text = str(raw or "").strip() or "."
    candidate = Path(text)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve()
    if not allow_escape:
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise PermissionError(f"path '{text}' escapes workspace root '{root}'") from exc
    return resolved


async def _run_process(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str = "",
) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdin=asyncio.subprocess.PIPE if stdin_text else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdin_bytes = stdin_text.encode("utf-8") if stdin_text else None
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(stdin_bytes), timeout=float(timeout))
    except asyncio.TimeoutError as exc:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            await proc.wait()
        except Exception:
            pass
        raise TimeoutError(f"command timed out after {timeout}s") from exc
    stdout_s = (stdout_b or b"").decode("utf-8", errors="replace")
    stderr_s = (stderr_b or b"").decode("utf-8", errors="replace")
    return proc.returncode or 0, stdout_s, stderr_s


def _run_process_sync(
    argv: list[str],
    *,
    cwd: Path,
    timeout: int,
    stdin_text: str = "",
) -> tuple[int, str, str]:
    return asyncio.run(_run_process(argv, cwd=cwd, timeout=timeout, stdin_text=stdin_text))


def _format_proc_output(rc: int, stdout_s: str, stderr_s: str) -> tuple[bool, str, str]:
    combined = stdout_s
    if stderr_s:
        combined = f"{combined}\n{stderr_s}".strip() if combined else stderr_s
    if rc == 0:
        return True, _bound_text(combined or "(no output)", MAX_OUTPUT_CHARS), ""
    err = _bound_text(combined or f"exit code {rc}", MAX_ERROR_CHARS)
    return False, _bound_text(stdout_s, MAX_OUTPUT_CHARS), err


def _exec_file_read(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_path(args.get("path"), root, allow_escape=bypass)
    if not path.is_file():
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    offset = max(1, int(args.get("offset") or 1))
    limit = max(1, min(500, int(args.get("limit") or 200)))
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    slice_lines = lines[offset - 1 : offset - 1 + limit]
    rendered = "\n".join(f"{offset + idx}: {line}" for idx, line in enumerate(slice_lines))
    return ClientToolExecutionResult(ok=True, output=_bound_text(rendered, _MAX_FILE_READ_CHARS))


def _exec_file_write(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_path(args.get("path"), root, allow_escape=bypass)
    content = str(args.get("content") or "")
    mode = str(args.get("mode") or "overwrite").strip().lower()
    if mode not in {"create", "overwrite", "append"}:
        return ClientToolExecutionResult(ok=False, error=f"unsupported file_write mode: {mode}")
    if mode == "create" and path.exists():
        return ClientToolExecutionResult(ok=False, error=f"file already exists: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    if mode == "append" and path.exists():
        with path.open("a", encoding="utf-8") as fh:
            fh.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    return ClientToolExecutionResult(ok=True, output=f"wrote {len(content)} chars to {path}")


def _exec_file_edit(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_path(args.get("path"), root, allow_escape=bypass)
    if not path.is_file():
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    old = str(args.get("old") or "")
    new = str(args.get("new") or "")
    if not old:
        return ClientToolExecutionResult(ok=False, error="'old' text must be non-empty")
    text = path.read_text(encoding="utf-8", errors="replace")
    count = text.count(old)
    if count == 0:
        return ClientToolExecutionResult(ok=False, error="target string not found in file")
    if count > 1:
        return ClientToolExecutionResult(ok=False, error=f"target string matched {count} times; must be unique")
    updated = text.replace(old, new, 1)
    path.write_text(updated, encoding="utf-8")
    return ClientToolExecutionResult(ok=True, output=f"edited {path}")


def _exec_file_delete(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_path(args.get("path"), root, allow_escape=bypass)
    if not path.exists():
        return ClientToolExecutionResult(ok=False, error=f"file not found: {path}")
    if path.is_dir():
        return ClientToolExecutionResult(ok=False, error="file_delete refuses directories")
    path.unlink()
    return ClientToolExecutionResult(ok=True, output=f"deleted {path}")


def _exec_directory_create(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    path = _resolve_path(args.get("path"), root, allow_escape=bypass)
    path.mkdir(parents=True, exist_ok=True)
    return ClientToolExecutionResult(ok=True, output=f"created directory {path}")


def _exec_file_find(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    base = _resolve_path(args.get("root", "."), root, allow_escape=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    pattern = str(args.get("pattern") or "*").strip() or "*"
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in {".git", "__pycache__", ".dart_tool", "node_modules"})
        for name in sorted(filenames):
            rel = (Path(dirpath) / name).relative_to(base).as_posix()
            if fnmatch.fnmatch(name, pattern) or fnmatch.fnmatch(rel, pattern):
                matches.append(rel)
                if len(matches) >= _MAX_FIND_MATCHES:
                    break
        if len(matches) >= _MAX_FIND_MATCHES:
            break
    return ClientToolExecutionResult(ok=True, output="\n".join(matches) or "(no matches)")


def _exec_directory_tree(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    base = _resolve_path(args.get("path", "."), root, allow_escape=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    depth = max(1, min(5, int(args.get("depth") or 2)))
    lines: list[str] = []

    def walk(cur: Path, level: int) -> None:
        if level > depth or len(lines) >= _MAX_TREE_ENTRIES:
            return
        try:
            entries = sorted(cur.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
        except OSError:
            return
        for entry in entries:
            if entry.name in {".git", "__pycache__"}:
                continue
            indent = "  " * (level - 1)
            suffix = "/" if entry.is_dir() else ""
            lines.append(f"{indent}{entry.name}{suffix}")
            if len(lines) >= _MAX_TREE_ENTRIES:
                return
            if entry.is_dir() and level < depth:
                walk(entry, level + 1)

    walk(base, 1)
    return ClientToolExecutionResult(ok=True, output="\n".join(lines) or "(empty)")


def _exec_text_search(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    query = str(args.get("query") or "")
    if not query:
        return ClientToolExecutionResult(ok=False, error="query is required")
    base = _resolve_path(args.get("root", "."), root, allow_escape=bypass)
    if not base.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {base}")
    glob_pat = str(args.get("glob") or "").strip()
    matches: list[str] = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in {".git", "__pycache__", ".dart_tool", "node_modules"})
        for name in sorted(filenames):
            if glob_pat and not fnmatch.fnmatch(name, glob_pat):
                continue
            fpath = Path(dirpath) / name
            try:
                if fpath.stat().st_size > 512 * 1024:
                    continue
                content = fpath.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = fpath.relative_to(base).as_posix()
            for lineno, line in enumerate(content.splitlines(), 1):
                if query in line:
                    matches.append(f"{rel}:{lineno}: {line.strip()[:200]}")
                    if len(matches) >= _MAX_SEARCH_MATCHES:
                        return ClientToolExecutionResult(ok=True, output="\n".join(matches))
    return ClientToolExecutionResult(ok=True, output="\n".join(matches) or "(no matches)")


def _exec_run_code(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    code = str(args.get("code") or "")
    if not code.strip():
        return ClientToolExecutionResult(ok=False, error="code is required")
    language = str(args.get("language") or "python").strip().lower()
    stdin_text = str(args.get("stdin") or "")
    timeout = max(1, min(60, int(args.get("timeout") or 15)))
    cwd = root
    if language in {"python", "py"}:
        rc, out, err = _run_process_sync(
            [sys.executable, "-c", code],
            cwd=cwd,
            timeout=timeout,
            stdin_text=stdin_text,
        )
        ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
        return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
    if language in {"js", "javascript", "node"}:
        rc, out, err = _run_process_sync(
            ["node", "-e", code],
            cwd=cwd,
            timeout=timeout,
            stdin_text=stdin_text,
        )
        ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
        return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
    if language in {"powershell", "ps1"}:
        with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8") as tmp:
            tmp.write(code)
            tmp_path = Path(tmp.name)
        try:
            rc, out, err = _run_process_sync(
                ["powershell", "-NoProfile", "-File", str(tmp_path)],
                cwd=cwd,
                timeout=timeout,
                stdin_text=stdin_text,
            )
            ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
            return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
        finally:
            tmp_path.unlink(missing_ok=True)
    if language in {"bash", "sh"}:
        rc, out, err = _run_process_sync(
            ["bash", "-lc", code],
            cwd=cwd,
            timeout=timeout,
            stdin_text=stdin_text,
        )
        ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
        return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)
    return ClientToolExecutionResult(ok=False, error=f"unsupported language on client: {language}")


def _exec_run_program(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    command = str(args.get("command") or "").strip()
    if not command:
        return ClientToolExecutionResult(ok=False, error="command is required")
    cwd = _resolve_path(args.get("cwd") or ".", root, allow_escape=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"working directory not found: {cwd}")
    timeout = max(1, min(120, int(args.get("timeout") or 30)))
    try:
        raw_tokens = shlex.split(command, posix=(os.name != "nt"))
    except ValueError as exc:
        return ClientToolExecutionResult(ok=False, error=f"invalid command syntax: {exc}")
    argv = [
        tok[1:-1] if len(tok) >= 2 and tok[0] == tok[-1] and tok[0] in {'"', "'"} else tok
        for tok in raw_tokens
    ]
    if not argv:
        return ClientToolExecutionResult(ok=False, error="command is required")
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=timeout)
    ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
    return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)


def _exec_test_run(args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    cwd = _resolve_path(args.get("root") or ".", root, allow_escape=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {cwd}")
    argv = [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "-q"]
    rel_path = str(args.get("path") or "").strip()
    if rel_path:
        target = _resolve_path(rel_path, cwd, allow_escape=bypass)
        argv.append(str(target))
    pattern = str(args.get("pattern") or "").strip()
    if pattern:
        argv.extend(["-k", pattern])
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=120)
    ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
    return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)


def _exec_git(tool: str, args: Mapping[str, Any], root: Path, *, bypass: bool) -> ClientToolExecutionResult:
    cwd = _resolve_path(args.get("cwd") or ".", root, allow_escape=bypass)
    if not cwd.is_dir():
        return ClientToolExecutionResult(ok=False, error=f"directory not found: {cwd}")
    argv: list[str]
    if tool == "git_status":
        argv = ["git", "status", "--short"]
    elif tool == "git_diff":
        argv = ["git", "diff"]
        if args.get("staged") is True:
            argv.append("--staged")
        rel_path = str(args.get("path") or "").strip()
        if rel_path:
            target = _resolve_path(rel_path, cwd, allow_escape=bypass)
            argv.extend(["--", str(target)])
    elif tool == "git_log":
        limit = max(1, min(100, int(args.get("limit") or 20)))
        argv = ["git", "log", f"-n{limit}", "--oneline"]
    else:
        argv = ["git", "branch", "-a"]
    rc, out, err = _run_process_sync(argv, cwd=cwd, timeout=30)
    ok, stdout_msg, err_msg = _format_proc_output(rc, out, err)
    return ClientToolExecutionResult(ok=ok, output=stdout_msg, error=err_msg)


def execute_client_tool(
    tool: str,
    args: Mapping[str, Any] | None,
    *,
    policy: ClientAgentPolicy,
    approved: bool = False,
) -> ClientToolExecutionResult:
    """Execute one client-delegatable tool locally under ``policy``."""
    clean_tool = str(tool or "").strip().lstrip("/")
    if clean_tool not in CLIENT_DELEGATABLE_TOOLS:
        return ClientToolExecutionResult(
            ok=False,
            error=f"tool '{clean_tool}' is not permitted on the client companion",
            denied=True,
            mode=policy.mode,
            risk="execute",
        )
    risk = tool_risk_of(clean_tool)
    if not approved and not mode_allows_without_prompt(policy.mode, risk):
        return ClientToolExecutionResult(
            ok=False,
            error=f"approval required on client PC for '{clean_tool}' ({risk}) in {policy.mode} mode",
            needs_approval=True,
            mode=policy.mode,
            risk=risk,
        )
    bypass = policy.mode == "bypass"
    safe_args = dict(args or {})
    try:
        if clean_tool == "file_read":
            res = _exec_file_read(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "file_write":
            res = _exec_file_write(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "file_edit":
            res = _exec_file_edit(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "file_delete":
            res = _exec_file_delete(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "directory_create":
            res = _exec_directory_create(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "file_find":
            res = _exec_file_find(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "directory_tree":
            res = _exec_directory_tree(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "text_search":
            res = _exec_text_search(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "run_code":
            res = _exec_run_code(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "run_program":
            res = _exec_run_program(safe_args, policy.workspace_root, bypass=bypass)
        elif clean_tool == "test_run":
            res = _exec_test_run(safe_args, policy.workspace_root, bypass=bypass)
        else:
            res = _exec_git(clean_tool, safe_args, policy.workspace_root, bypass=bypass)
    except (PermissionError, OSError, TimeoutError, ValueError) as exc:
        return ClientToolExecutionResult(
            ok=False,
            error=str(exc),
            mode=policy.mode,
            risk=risk,
        )
    return ClientToolExecutionResult(
        ok=res.ok,
        output=res.output,
        error=res.error,
        denied=res.denied,
        needs_approval=res.needs_approval,
        mode=policy.mode,
        risk=risk,
    )
