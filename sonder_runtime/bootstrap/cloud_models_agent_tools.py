"""The read-only ``cloud_models_status`` MCP tool (auto-discovered ``*_agent_tools`` registrar).

It reports the last cloud model refresh, the effective retired list and its
sources, newly seen catalog models, the operator ladder state and pending
operator choices.  It reads the refresh state files, the model lists and the
environment only: no network, no writes.  Applying a refresh is the CLI's or
the daily schedule's job, never this tool's.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from typing import Any, Protocol, TypeVar

from ..adapters.cloud_model_refresh import status_payload
from ..adapters.debug_tools_executor import default_path_redactor, redact_paths

_F = TypeVar("_F", bound=Callable[..., object])


class _McpServer(Protocol):
    """The one method of the MCP server object that ``register`` uses."""

    def tool(self) -> Callable[[_F], _F]: ...


def _redacted(payload: dict[str, Any], redact: Callable[[str], str]) -> dict[str, Any]:
    """Model-visible copy of ``payload``: host paths in ``paths``, issues and errors redacted."""
    result = dict(payload)
    paths = result.get("paths")
    if isinstance(paths, dict):
        result["paths"] = {
            key: redact(value) if isinstance(value, str) else value for key, value in paths.items()
        }
    issues = result.get("model_lists_issues")
    if isinstance(issues, list):
        result["model_lists_issues"] = [
            redact_paths(item, redact) if isinstance(item, str) else item for item in issues
        ]
    for key in ("preferences_error", "state_error", "error"):
        value = result.get(key)
        if isinstance(value, str):
            result[key] = redact_paths(value, redact)
    return result


def run_status() -> dict[str, Any]:
    """The status payload with host paths redacted, or ``{"ok": false, "error": ...}``; never raises.

    Absolute paths (the state home sits under the user profile) go through the
    same display redactor as the other model-visible inventory tools.
    """
    try:
        payload = status_payload()
    except Exception as exc:  # noqa: BLE001 - a tool reports, it never crashes the server
        payload = {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}
    return _redacted(payload, default_path_redactor())


def register(mcp: _McpServer, record: Callable[..., Any] | None) -> None:
    """Register ``cloud_models_status`` without touching the filesystem at startup."""

    @mcp.tool()
    def cloud_models_status() -> str:
        """Report Sonder's Ollama cloud model refresh: last run, retired models and their source, new catalog models, ladder state per cloud tier, pending operator choices and the effective cloud tier models. Read-only: no network, no writes; apply a refresh with `python -m sonder_runtime models cloud-refresh --apply` or the daily schedule."""
        started = time.time()
        payload = run_status()
        output = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        if record is not None:
            record("cloud_models_status", {}, ok=bool(payload.get("ok")), started=started,
                   summary=str(payload.get("error") or "ok")[:200], output=output)
        return output


__all__ = ["register", "run_status"]
