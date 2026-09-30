"""Read-only OpenRouter MCP tools: ``openrouter_models`` and ``openrouter_account``.

Both are network reads against OpenRouter's catalog and key endpoints.  They
send the API key (never a prompt), spend no credits, and change nothing, so
they are graded like the other network-reading tools (``safe`` in
``command_catalog._READ_ONLY``).  Each call still passes the cloud consent
gate: with ``SONDER_ALLOW_CLOUD`` off they refuse before any byte is sent.
The tier *mapping* is deliberately not a model-facing tool: it is written only
by the operator's ``python -m sonder_runtime openrouter use`` command.

server.py is size-capped, so the tools register themselves from here with
the same ``@mcp.tool()`` decorator (see ``computer_use_tools``).
"""
from __future__ import annotations

import json
import time

from ..domain.common.errors import SonderError

MAX_LISTED = 200


def _json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _gateway():
    from ..adapters.inference.openrouter_gateway import OpenRouterGateway

    return OpenRouterGateway()


def models_payload(search: str = "", tools: bool = False, limit: int = 50, *, gateway=None) -> dict:
    listing = (gateway or _gateway()).list_models(search=str(search or ""), tools=bool(tools))
    bounded = max(1, min(int(limit or 50), MAX_LISTED))
    return {"ok": True, **listing, "models": listing["models"][:bounded],
            "truncated": len(listing["models"]) > bounded}


def account_payload(*, gateway=None) -> dict:
    return {"ok": True, **(gateway or _gateway()).account()}


def register(mcp, record) -> None:
    """Register the tools; ``record`` is the server's ``_record_direct_tool``."""

    def run(name, args, body):
        started = time.time()
        try:
            payload = body()
        except SonderError as exc:
            payload = {"ok": False, "error": str(exc)}
        except Exception as exc:  # noqa: BLE001 - a tool reports, it never crashes the server
            payload = {"ok": False, "error": "%s: %s" % (type(exc).__name__, exc)}
        output = _json(payload)
        record(name, args, ok=bool(payload.get("ok")), started=started,
               summary=str(payload.get("error") or "ok")[:200], output=output)
        return output

    @mcp.tool()
    def openrouter_models(search: str = "", tools: bool = False, limit: int = 50) -> str:
        """List OpenRouter models this key may use: id, context, USD per 1M prompt/completion tokens, tool and structured-output support. Read-only; needs cloud opt-in."""
        args = {"search": search, "tools": tools, "limit": limit}
        return run("openrouter_models", args, lambda: models_payload(search, tools, limit))

    @mcp.tool()
    def openrouter_account() -> str:
        """Show OpenRouter credits remaining, this key's limit and usage. Read-only; needs cloud opt-in."""
        return run("openrouter_account", {}, account_payload)


__all__ = ["account_payload", "models_payload", "register"]
