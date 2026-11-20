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

_F = TypeVar("_F", bound=Callable[..., object])


class _McpServer(Protocol):
    """The one method of the MCP server object that ``register`` uses."""

    def tool(self) -> Callable[[_F], _F]: ...


def run_status() -> dict[str, Any]:
    """The status payload, or ``{"ok": false, "error": ...}``; never raises."""
    try:
        return status_payload()
    except Exception as exc:  # noqa: BLE001 - a tool reports, it never crashes the server
        return {"ok": False, "error": "%s: %s" % (type(exc).__name__, str(exc)[:200])}


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
