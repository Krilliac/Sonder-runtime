"""The read-only ``claim_check`` tool: check a model's code claim before anyone acts on it.

One executor (``run_claim_check``) backs both the legacy MCP registrar and the
native MCP handler. It never writes, never runs anything, and returns typed
JSON: ``{"ok": true, "reports": [...], "summary": {...}}`` or
``{"ok": false, "error": {"code", "message"}}``.

The ``root`` must be an authorized repository root (the operator's guarded
file roots), and every file the checker would read passes the same read guard
as the other repository read tools, so credentials and control state are
neither checked nor quoted back as evidence.
"""
from __future__ import annotations

import json
import os
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from ..adapters.claim_checker import ClaimChecker, claim_from_mapping
from ..domain.claim_check import ClaimError
from ..platform.claim_check_config import ClaimCheckConfig

MAX_OUTPUT_CHARS = 200_000
_VERDICTS = ("reject", "flag", "keep")


def _error(code: str, message: str) -> dict:
    return {"ok": False, "error": {"code": code, "message": message[:300]}}


def _default_root_guard(root: str) -> str:
    from ..adapters.filesystem.file_ops import resolve_repository_read_path
    return str(resolve_repository_read_path(root, allow_workspace_root=True))


def _default_file_guard(root: str) -> Callable[[str], str]:
    from ..adapters.filesystem.file_ops import resolve_repository_read_path

    def guard(path: str) -> str:
        return str(resolve_repository_read_path(path, extra_roots=root))
    return guard


def _default_config() -> ClaimCheckConfig:
    from .config_loading import load_config_or_none
    config = load_config_or_none()
    return getattr(config, "claim_check", None) or ClaimCheckConfig()


def _claims_from_arguments(arguments: Mapping) -> list[dict]:
    raw = arguments.get("claims_json")
    single = arguments.get("file")
    if raw not in (None, "") and single not in (None, ""):
        raise ValueError("pass either file (one claim) or claims_json (a batch), not both")
    if raw in (None, ""):
        if single is None:
            raise ValueError("pass file (one claim) or claims_json (a JSON array of claims)")
        row = {"file": single, "text": arguments.get("claim_text") or "",
               "category": arguments.get("category") or "", "guard": arguments.get("guard") or "",
               "entry_point": arguments.get("entry_point") or "",
               "claim_id": arguments.get("claim_id") or "",
               "symbols": arguments.get("symbols") or ""}
        if arguments.get("line"):
            row["line_start"] = arguments["line"]
        if arguments.get("line_end"):
            row["line_end"] = arguments["line_end"]
        return [row]
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as exc:
            raise ValueError("claims_json is not valid JSON: %s" % exc) from None
    if not isinstance(raw, list):
        raise ValueError("claims_json must be a JSON array of claim objects")
    return raw


def run_claim_check(arguments: Mapping, *, config: ClaimCheckConfig | None = None,
                    root_guard: Callable[[str], str] | None = None,
                    file_guard: Callable[[str], str] | None = None) -> dict:
    """Check one claim or a batch; returns a JSON-serializable dict, never raises."""
    if not isinstance(arguments, Mapping):
        return _error("invalid_arguments", "arguments must be an object")
    root = arguments.get("root")
    if not isinstance(root, str) or not root.strip():
        return _error("invalid_arguments", "root (the repository root) is required")
    try:
        rows = _claims_from_arguments(arguments)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc))
    config = config or _default_config()
    if not rows:
        return _error("invalid_arguments", "claims_json holds no claims")
    if len(rows) > config.max_batch_claims:
        return _error("too_many_claims",
                      "at most max_batch_claims=%d claims per call" % config.max_batch_claims)
    try:
        claims = [claim_from_mapping(row) for row in rows]
    except ClaimError as exc:
        return _error("invalid_claim", str(exc))
    try:
        resolved = (root_guard or _default_root_guard)(root.strip())
        if not os.path.isdir(resolved):
            raise ValueError("the repository root is not a directory")
    except (OSError, ValueError, PermissionError) as exc:
        return _error("root_refused", str(exc))
    guard_for_files = file_guard or _default_file_guard(str(resolved))

    def read_guard(path: str) -> bool:
        try:
            guard_for_files(path)
            return True
        except (OSError, ValueError, PermissionError):
            return False

    reports = ClaimChecker(resolved, config, read_guard=read_guard).check_many(claims)
    verdicts = {name: 0 for name in _VERDICTS}
    duplicates = 0
    for report in reports:
        verdicts[report.verdict.value] += 1
        duplicates += bool(report.duplicate_of)
    body = _fit_reports(reports)
    omitted = len(reports) - len(body)
    out = {"ok": True, "reports": body,
           "summary": {"total": len(reports), "duplicates": duplicates, "verdicts": verdicts}}
    if omitted:
        out["summary"]["omitted_reports"] = omitted
    return out


def _fit_reports(reports) -> list[dict]:
    """Leading reports whose JSON array fits MAX_OUTPUT_CHARS; each is serialized once."""
    body: list[dict] = []
    used = 2  # "[" and "]"
    for report in reports:
        row = report.to_dict()
        used += len(json.dumps(row, ensure_ascii=False)) + (2 if body else 0)  # ", " separator
        if used > MAX_OUTPUT_CHARS:
            break
        body.append(row)
    return body


def _within_roots(roots) -> Callable[[str], str]:
    resolved = [Path(os.path.realpath(r)) for r in roots]

    def guard(root: str) -> str:
        target = Path(os.path.realpath(Path(root).expanduser()))
        if not any(target == base or target.is_relative_to(base) for base in resolved):
            raise PermissionError("claim_check root is outside the native workspace roots")
        return str(target)
    return guard


def native_claim_check(arguments: Mapping, roots=None, *, config: ClaimCheckConfig | None = None,
                       root_guard=None, file_guard=None) -> dict:
    """Native transport handler, entered after its schema and permission gate."""
    if roots is not None and root_guard is None:
        root_guard = _within_roots(roots)
    result = run_claim_check(arguments, config=config, root_guard=root_guard,
                             file_guard=file_guard)
    failed = not result["ok"]
    return {"output": json.dumps(result, ensure_ascii=False), "isError": failed,
            "error": "tool_refused" if failed else None,
            "evidence": {"tool": "claim_check"}}


def register(mcp, record, project_root=None, *, config_provider=None, root_guard=None,
             file_guard=None) -> None:
    """Register ``claim_check`` without touching the filesystem at startup."""

    @mcp.tool()
    def claim_check(root: str, file: str = "", line: int = 0, line_end: int = 0,
                    symbols: str = "", category: str = "", guard: str = "",
                    entry_point: str = "", claim_text: str = "", claim_id: str = "",
                    claims_json: str = "") -> str:
        """Check a model's code claim against the repository, read-only: is the cited file,
        line and are the named symbols real, and is a guard of the claimed category nearby
        (verdict reject = the citation is wrong; flag = a guard-shaped line was found nearby, which
        says nothing about whether the claim is true; keep = nothing contradicts the citation).
        Pass file (+ line, symbols, category, guard, entry_point, claim_text) or claims_json."""
        started = time.time()
        args = {"root": root, "file": file, "line": line, "line_end": line_end,
                "symbols": symbols, "category": category, "guard": guard,
                "entry_point": entry_point, "claim_text": claim_text, "claim_id": claim_id,
                "claims_json": claims_json}
        try:
            config = config_provider() if config_provider is not None else None
            result = run_claim_check(
                {key: value for key, value in args.items() if value not in ("", 0)},
                config=config, root_guard=root_guard, file_guard=file_guard)
        except Exception as exc:  # a tool reports failures rather than taking down MCP
            result = _error("internal_error", "%s: %s" % (type(exc).__name__, exc))
        output = json.dumps(result, ensure_ascii=False)
        if record is not None:
            summary = (str(result["summary"]["verdicts"]) if result["ok"]
                       else result["error"]["code"])
            record("claim_check", {"root": root, "file": file, "line": line}, ok=result["ok"],
                   started=started, summary=summary[:200], output=output)
        return output


__all__ = ["native_claim_check", "register", "run_claim_check"]
