"""Authenticated HTTP presentation for the bounded A2A JSON-RPC seam."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ...a2a.jsonrpc import A2AJsonRpcTransport


@dataclass(frozen=True, slots=True)
class A2AJsonRpcHttpResult:
    body: dict[str, Any]
    status_code: int = 200


def dispatch_a2a_jsonrpc_route(
    handler: object,
    method: str,
    path: str,
    payload: Mapping[str, Any] | str | bytes,
) -> A2AJsonRpcHttpResult | None:
    """Dispatch the explicitly configured local A2A JSON-RPC endpoint."""
    if path != "/a2a":
        return None
    if method != "POST":
        return A2AJsonRpcHttpResult(
            {"error": {"code": -32600, "message": "method not allowed"}}, 405
        )
    if not callable(handler):
        return A2AJsonRpcHttpResult(
            {"error": {"code": "A2A_UNAVAILABLE", "message": "A2A handler is not configured"}},
            503,
        )
    response = A2AJsonRpcTransport(handler).handle(payload)
    return A2AJsonRpcHttpResult(response)


__all__ = ["A2AJsonRpcHttpResult", "dispatch_a2a_jsonrpc_route"]
