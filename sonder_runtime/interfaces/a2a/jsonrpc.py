"""Bounded A2A JSON-RPC binding for application-owned task operations.

The transport validates the wire envelope and delegates the actual task
operation to an injected application handler. It never reads the local task
store, fetches an Agent Card, or treats a remote request as an authorization
grant. Authentication and capability admission remain host responsibilities.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping


MAX_A2A_METHOD_LENGTH = 64
SUPPORTED_METHODS = frozenset({
    "SendMessage",
    "GetTask",
    "ListTasks",
    "CancelTask",
    "GetExtendedAgentCard",
})


@dataclass(frozen=True, slots=True)
class A2AJsonRpcLimits:
    max_request_bytes: int = 256_000
    max_response_bytes: int = 256_000

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (self.max_request_bytes, self.max_response_bytes)
        ):
            raise ValueError("A2A JSON-RPC limits must be positive integers")


A2ARequestHandler = Callable[[str, Mapping[str, Any]], Mapping[str, Any]]


class A2AJsonRpcTransport:
    """Validate and dispatch one bounded A2A JSON-RPC request."""

    def __init__(
        self,
        handler: A2ARequestHandler,
        *,
        limits: A2AJsonRpcLimits | None = None,
    ) -> None:
        if not callable(handler):
            raise TypeError("A2A handler must be callable")
        self._handler = handler
        self._limits = limits or A2AJsonRpcLimits()

    def handle(self, request: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
        value = self._decode(request)
        request_id = value.get("id")
        if (
            value.get("jsonrpc") != "2.0"
            or not self._valid_id(request_id)
            or set(value) - {"jsonrpc", "id", "method", "params"}
            or not isinstance(value.get("method"), str)
        ):
            return self._error(request_id, -32600, "invalid request")
        method = value["method"]
        params = value.get("params", {})
        if not method or len(method) > MAX_A2A_METHOD_LENGTH or not isinstance(params, Mapping):
            return self._error(request_id, -32602, "invalid params")
        if method not in SUPPORTED_METHODS:
            return self._error(request_id, -32601, "method not found")
        try:
            result = self._handler(method, params)
        except (KeyError, TypeError, ValueError):
            return self._error(request_id, -32602, "invalid params")
        except Exception:
            return self._error(request_id, -32603, "internal A2A handler error")
        if not isinstance(result, Mapping):
            return self._error(request_id, -32603, "handler returned an invalid result")
        response = {"jsonrpc": "2.0", "id": request_id, "result": dict(result)}
        try:
            encoded = json.dumps(response, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return self._error(request_id, -32603, "handler returned an invalid result")
        if len(encoded.encode("utf-8")) > self._limits.max_response_bytes:
            return self._error(request_id, -32603, "A2A response exceeds its bound")
        return response

    def _decode(self, request: Mapping[str, Any] | str | bytes) -> Mapping[str, Any]:
        if isinstance(request, bytes):
            if len(request) > self._limits.max_request_bytes:
                return {"id": None}
            try:
                request = request.decode("utf-8")
            except UnicodeDecodeError:
                return {"id": None}
        if isinstance(request, str):
            if len(request.encode("utf-8")) > self._limits.max_request_bytes:
                return {"id": None}
            try:
                request = json.loads(request)
            except (TypeError, ValueError, json.JSONDecodeError):
                return {"id": None}
        if not isinstance(request, Mapping):
            return {"id": None}
        try:
            encoded = json.dumps(request, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            return {"id": None}
        if len(encoded.encode("utf-8")) > self._limits.max_request_bytes:
            return {"id": None}
        return request

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return isinstance(value, (str, int, float)) and not isinstance(value, bool)

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


__all__ = ["A2AJsonRpcLimits", "A2AJsonRpcTransport", "SUPPORTED_METHODS"]
