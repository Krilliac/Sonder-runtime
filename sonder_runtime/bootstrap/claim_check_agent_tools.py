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

import contextlib
import functools
import json
import os
import time
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any, Protocol, TypeVar

from ..adapters.claim_checker import ClaimChecker, claim_from_mapping
from ..application.ports.tool_registry import ToolSchemaSelection
from ..domain.claim_check import ClaimError, ClaimReport
from ..interfaces.mcp.transport import McpTransportLimits
from ..platform.claim_check_config import ClaimCheckConfig

MAX_OUTPUT_CHARS = 200_000
# Room kept inside a native frame for the JSON-RPC envelope around the tool result
# ({"jsonrpc","id","result"}); request ids are short in practice. This is the envelope
# only: the evidence the native execute() wrapper adds is measured, not reserved here
# (see ``_wrapper_evidence``).
_FRAME_ENVELOPE_BYTES = 1_024
# Upper bound on a registered tool name; native_mcp imports this module, so the registry
# cannot be read from here, and a test pins every registered name under the bound.
_MAX_TOOL_NAME_CHARS = 64
_MAX_SELECTED_TOOLS = 8  # ToolDiscovery.load accepts at most 8 names per selection
_DIGEST_HEX = "0" * 64  # inventory_digest and the selection_id are sha256 hex
_ITEM_SEPARATOR = ", "  # json.dumps' default item separator between array elements
_VERDICTS = ("reject", "flag", "keep")

_F = TypeVar("_F", bound=Callable[..., object])


class _McpServer(Protocol):
    """The one method of the MCP server object that ``register`` uses."""

    def tool(self) -> Callable[[_F], _F]: ...


def _error(code: str, message: str) -> dict[str, Any]:
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


def _claims_from_arguments(arguments: Mapping[str, Any]) -> list[dict[str, Any]]:
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


def _check_reports(arguments: Mapping[str, Any], config: ClaimCheckConfig | None,
                   root_guard: Callable[[str], str] | None,
                   file_guard: Callable[[str], str] | None) -> tuple[dict[str, Any] | None, list[ClaimReport]]:
    """Run the checks: ``(error, [])`` on a refused request, else ``(None, reports)``."""
    if not isinstance(arguments, Mapping):
        return _error("invalid_arguments", "arguments must be an object"), []
    root = arguments.get("root")
    if not isinstance(root, str) or not root.strip():
        return _error("invalid_arguments", "root (the repository root) is required"), []
    try:
        rows = _claims_from_arguments(arguments)
    except ValueError as exc:
        return _error("invalid_arguments", str(exc)), []
    config = config or _default_config()
    if not rows:
        return _error("invalid_arguments", "claims_json holds no claims"), []
    if len(rows) > config.max_batch_claims:
        return _error("too_many_claims",
                      "at most max_batch_claims=%d claims per call" % config.max_batch_claims), []
    try:
        claims = [claim_from_mapping(row) for row in rows]
    except ClaimError as exc:
        return _error("invalid_claim", str(exc)), []
    try:
        resolved = (root_guard or _default_root_guard)(root.strip())
        if not os.path.isdir(resolved):
            raise ValueError("the repository root is not a directory")
    except (OSError, ValueError, PermissionError) as exc:
        return _error("root_refused", str(exc)), []
    guard_for_files = file_guard or _default_file_guard(str(resolved))

    def read_guard(path: str) -> bool:
        try:
            guard_for_files(path)
            return True
        except (OSError, ValueError, PermissionError):
            return False

    with _read_policy_scope(file_guard is None):
        return None, ClaimChecker(resolved, config, read_guard=read_guard).check_many(claims)


def _read_policy_scope(default_guard: bool) -> contextlib.AbstractContextManager[Any]:
    """The default guard re-derives the control-plane inventory per call; resolve it once.

    A host-supplied guard owns its own cost and is left alone."""
    if not default_guard:
        return contextlib.nullcontext()
    from ..adapters.filesystem.file_ops import read_policy_snapshot
    return read_policy_snapshot()


def _assemble(reports: list[ClaimReport], body: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = {name: 0 for name in _VERDICTS}
    duplicates = 0
    for report in reports:
        verdicts[report.verdict.value] += 1
        duplicates += bool(report.duplicate_of)
    out: dict[str, Any] = {"ok": True, "reports": body,
           "summary": {"total": len(reports), "duplicates": duplicates, "verdicts": verdicts}}
    omitted = len(reports) - len(body)
    if omitted:
        out["summary"]["omitted_reports"] = omitted
    return out


def run_claim_check(arguments: Mapping[str, Any], *, config: ClaimCheckConfig | None = None,
                    root_guard: Callable[[str], str] | None = None,
                    file_guard: Callable[[str], str] | None = None) -> dict[str, Any]:
    """Check one claim or a batch; returns a JSON-serializable dict. Refusals are typed
    errors; an unexpected fault in the checker propagates to the caller."""
    error, reports = _check_reports(arguments, config, root_guard, file_guard)
    if error is not None:
        return error
    return _assemble(reports, _fit_reports(reports, MAX_OUTPUT_CHARS - 2, len))  # "[" and "]"


def _fit_reports(reports: list[ClaimReport], budget: int,
                 cost: Callable[[str], int]) -> list[dict[str, Any]]:
    """Leading reports whose cost fits ``budget``; each is serialized once.

    ``cost(text)`` is what the JSON text of one report (or the ", " separator) adds to
    the final output; costs are additive, so the prefix is found in one pass."""
    body: list[dict[str, Any]] = []
    used = 0
    separator = cost(_ITEM_SEPARATOR)
    for report in reports:
        row = report.to_dict()
        used += cost(json.dumps(row, ensure_ascii=False)) + (separator if body else 0)
        if used > budget:
            break
        body.append(row)
    return body


def _wire_cost(text: str) -> int:
    """Bytes ``text`` adds to a native frame, where the tool output is embedded twice.

    Once as the ``output`` string of ``structuredContent`` and once more inside the
    ``text`` item, whose JSON is escaped a second time; the frame is ASCII-escaped."""
    ascii_once = len(json.dumps(text)) - 2
    escaped = json.dumps(text, ensure_ascii=False)[1:-1]
    return ascii_once + len(json.dumps(escaped)) - 2


@functools.cache
def _wrapper_evidence() -> dict[str, Any]:
    """The evidence native ``execute()`` adds after ``native_claim_check`` returns, at its
    worst case: the progressive-tools schema-selection marker (``_MAX_SELECTED_TOOLS``
    names at ``_MAX_TOOL_NAME_CHARS``, a 64-hex selection id), the
    inventory digest and the compatibility flag."""
    names = {str(index).ljust(_MAX_TOOL_NAME_CHARS, "x") for index in range(_MAX_SELECTED_TOOLS)}
    marker = ToolSchemaSelection(frozenset(names), selection_id=_DIGEST_HEX).marker()
    return {"tool_schema_selection": marker, "inventory_digest": _DIGEST_HEX,
            "native_compatibility_path": True}


def native_frame_bytes(result: Mapping[str, Any]) -> int:
    """Encoded size of a native tool result as the standard MCP transport writes it,
    without the JSON-RPC envelope (``_FRAME_ENVELOPE_BYTES`` covers that)."""
    standard = {
        "content": [{"type": "text",
                     "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}],
        "structuredContent": result,
    }
    return len(json.dumps(standard, separators=(",", ":"), ensure_ascii=True).encode("utf-8"))


def _within_roots(roots: Iterable[str | os.PathLike[str]]) -> Callable[[str], str]:
    resolved = [Path(os.path.realpath(r)) for r in roots]

    def guard(root: str) -> str:
        from ..adapters.filesystem.file_ops import _reject_network_path
        # Lexical refusal first: realpath on a UNC/device spelling opens an SMB
        # connection (and can leak NTLM credentials) before containment is checked.
        root = _reject_network_path(root)
        expanded = _reject_network_path(str(Path(root).expanduser()))
        target = Path(os.path.realpath(expanded))
        if not any(target == base or target.is_relative_to(base) for base in resolved):
            raise PermissionError("claim_check root is outside the native workspace roots")
        return str(target)
    return guard


def _native_result(result: dict[str, Any]) -> dict[str, Any]:
    failed = not result["ok"]
    return {"output": json.dumps(result, ensure_ascii=False), "isError": failed,
            "error": "tool_refused" if failed else None,
            "evidence": {"tool": "claim_check"}}


def native_claim_check(arguments: Mapping[str, Any],
                       roots: Iterable[str | os.PathLike[str]] | None = None, *,
                       config: ClaimCheckConfig | None = None,
                       root_guard: Callable[[str], str] | None = None,
                       file_guard: Callable[[str], str] | None = None,
                       max_frame_bytes: int | None = None) -> dict[str, Any]:
    """Native transport handler, entered after its schema and permission gate.

    The reports are cut so the whole encoded response frame stays within
    ``max_frame_bytes`` (default: the transport's limit); the cut is reported as
    ``summary.omitted_reports``."""
    if roots is not None and root_guard is None:
        root_guard = _within_roots(roots)
    error, reports = _check_reports(arguments, config, root_guard, file_guard)
    if error is not None:
        return _native_result(error)
    limit = McpTransportLimits().max_frame_bytes if max_frame_bytes is None else max_frame_bytes
    # Overhead of everything but the reports; omitted_reports is counted at its maximum.
    # Native execute() and the transport wrap this result afterwards, so the frame is
    # measured with that wrapper's worst-case evidence (written twice by
    # StdioMcpTransport._standard_tool_result) plus the JSON-RPC envelope reserve.
    shell = _native_result(_assemble(reports, []))
    shell["evidence"] = {**shell["evidence"], **_wrapper_evidence()}
    fixed = native_frame_bytes(shell)
    budget = limit - _FRAME_ENVELOPE_BYTES - fixed
    return _native_result(_assemble(reports, _fit_reports(reports, budget, _wire_cost)))


def register(mcp: _McpServer, record: Callable[..., Any] | None, project_root: object = None, *,
             config_provider: Callable[[], ClaimCheckConfig | None] | None = None,
             root_guard: Callable[[str], str] | None = None,
             file_guard: Callable[[str], str] | None = None) -> None:
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
