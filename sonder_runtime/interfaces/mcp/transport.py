"""Bounded JSON-RPC transport for the provider-neutral MCP contracts."""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass
from typing import Any, Mapping, TextIO

from ...application.protocol.mcp_compatibility import (
    McpCompatibility, McpNegotiation, McpNegotiationError,
    SubscriptionNotificationRouter,
)
from ...application.tools.generated_catalogs import GeneratedCatalogs
from ...application.ports.tool_registry import ToolDescriptor


@dataclass(frozen=True)
class McpTransportLimits:
    max_frame_bytes: int = 256_000
    max_tools: int = 256
    max_arguments_bytes: int = 64_000

    def __post_init__(self) -> None:
        if any(isinstance(v, bool) or v <= 0 for v in (
            self.max_frame_bytes, self.max_tools, self.max_arguments_bytes
        )):
            raise ValueError("MCP transport limits must be positive integers")


class McpTransportError(ValueError):
    """A bounded transport or protocol violation."""


class StdioMcpTransport:
    """Serve newline-delimited JSON-RPC over injectable text-like streams."""

    def __init__(
        self, input_stream: TextIO, output_stream: TextIO, *,
        compatibility: McpCompatibility, tool_catalog: Any = (), tool_handler: Any,
        notifications: SubscriptionNotificationRouter | None = None,
        connection_id: str = "stdio", limits: McpTransportLimits | None = None,
    ) -> None:
        if not connection_id or not callable(getattr(input_stream, "readline", None)):
            raise ValueError("MCP transport requires an input stream and connection id")
        if not callable(getattr(output_stream, "write", None)):
            raise ValueError("MCP transport requires a writable output stream")
        self._input, self._output = input_stream, output_stream
        self._compatibility, self._handler = compatibility, tool_handler
        self._router, self._connection_id = notifications, connection_id
        self._limits = limits or McpTransportLimits()
        self._negotiation: McpNegotiation | None = None
        self._write_lock = threading.Lock()
        self._catalog = self._build_catalog(tool_catalog)
        if len(self._catalog) > self._limits.max_tools:
            raise McpTransportError("tool catalog exceeds max_tools")

    def _build_catalog(self, source: Any) -> tuple[dict[str, Any], ...]:
        if hasattr(source, "mcp"):
            source = source.mcp
        if isinstance(source, Mapping) and "tools" in source:
            tools = source["tools"]
        elif hasattr(source, "list_all"):
            tools = GeneratedCatalogs.generate(source).mcp["tools"]
        else:
            tools = source
        result = []
        for tool in tools or ():
            if isinstance(tool, Mapping):
                item = dict(tool)
                item.setdefault("inputSchema", {"type": "object"})
                item.setdefault("description", "")
                item.setdefault("name", "")
            elif isinstance(tool, ToolDescriptor):
                item = {"name": tool.name, "description": tool.description,
                        "inputSchema": dict(tool.input_schema or {"type": "object"})}
            else:
                item = {"name": str(getattr(tool, "name", "")),
                        "description": str(getattr(tool, "description", "")),
                        "inputSchema": dict(getattr(tool, "input_schema", {}) or {"type": "object"})}
            if not isinstance(item["name"], str) or not item["name"].strip():
                raise McpTransportError("tool catalog contains an invalid name")
            result.append(item)
        names = [item["name"] for item in result]
        if len(set(names)) != len(names):
            raise McpTransportError("tool catalog contains duplicate names")
        return tuple(sorted(result, key=lambda item: item["name"]))

    @property
    def negotiation(self) -> McpNegotiation | None:
        return self._negotiation

    def serve(self) -> int:
        count = 0
        while True:
            raw = self._input.readline(self._limits.max_frame_bytes + 1)
            if raw in (b"", ""):
                break
            if raw.strip() == b"" if isinstance(raw, bytes) else raw.strip() == "":
                continue
            count += 1
            try:
                response = self._dispatch(self._decode_frame(raw))
            except McpTransportError as exc:
                response = self._error(None, -32700, str(exc))
            if response is not None:
                self._write(response)
        if self._router is not None:
            self._router.unsubscribe(self._connection_id)
        return count

    def _decode_frame(self, raw: str | bytes) -> dict[str, Any]:
        if isinstance(raw, bytes):
            if len(raw) > self._limits.max_frame_bytes:
                raise McpTransportError("MCP frame exceeds max_frame_bytes")
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise McpTransportError("MCP frame is not UTF-8") from exc
        if len(raw.encode("utf-8")) > self._limits.max_frame_bytes:
            raise McpTransportError("MCP frame exceeds max_frame_bytes")
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError) as exc:
            raise McpTransportError("invalid JSON-RPC frame") from exc
        if not isinstance(value, dict):
            raise McpTransportError("JSON-RPC frame must be an object")
        return value

    def _dispatch(self, request: dict[str, Any]) -> dict[str, Any] | None:
        request_id = request.get("id")
        notification = "id" not in request
        if request.get("jsonrpc") != "2.0" or (not notification and not self._valid_id(request_id)):
            return self._error(request_id, -32600, "invalid request")
        method = request.get("method")
        if not isinstance(method, str) or not method or len(method) > 128:
            return self._error(request_id, -32600, "invalid method")
        params = request.get("params", {})
        if not isinstance(params, dict):
            return self._error(request_id, -32602, "params must be an object")
        try:
            result = self._call(method, params)
        except McpNegotiationError as exc:
            return self._error(request_id, -32001, str(exc))
        except McpTransportError as exc:
            return self._error(request_id, -32602, str(exc))
        except KeyError as exc:
            return self._error(request_id, -32601, str(exc))
        except Exception:
            return self._error(request_id, -32603, "internal MCP handler error")
        if notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            versions = params.get("protocolVersions", params.get("protocolVersion"))
            if isinstance(versions, str):
                versions = (versions,)
            if not isinstance(versions, (list, tuple)) or not all(isinstance(v, str) for v in versions):
                raise McpNegotiationError("initialize requires protocolVersions")
            capabilities = params.get("capabilities", {})
            if not isinstance(capabilities, dict):
                raise McpNegotiationError("client capabilities must be an object")
            self._negotiation = self._compatibility.negotiate(
                tuple(versions), client_capabilities=tuple(str(k) for k in capabilities)
            )
            return {"protocolVersion": self._negotiation.agreed_version,
                    "capabilities": {name: {} for name in self._negotiation.capabilities},
                    "serverInfo": {"name": "sonder-runtime", "version": self._negotiation.server_version}}
        if self._negotiation is None:
            raise McpTransportError("initialize is required")
        if method == "notifications/initialized":
            return {}
        if method == "tools/list":
            return {"tools": list(self._catalog)}
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            if not isinstance(name, str) or not name:
                raise McpTransportError("tools/call requires name")
            if not isinstance(arguments, dict):
                raise McpTransportError("tools/call arguments must be an object")
            if len(json.dumps(arguments, separators=(",", ":")).encode("utf-8")) > self._limits.max_arguments_bytes:
                raise McpTransportError("tool arguments exceed max_arguments_bytes")
            value = self._handler(name, arguments) if callable(self._handler) else self._handler.handle(name, arguments)
            return dict(value) if isinstance(value, Mapping) else {"output": value}
        if method == "sonder/subscribe":
            event = params.get("event")
            if self._router is None or not isinstance(event, str) or not event:
                raise McpTransportError("subscription router and event are required")
            self._router.subscribe(self._connection_id, event, self._on_notification)
            return {"subscribed": event}
        if method == "sonder/unsubscribe":
            event = params.get("event")
            if self._router is not None:
                self._router.unsubscribe(self._connection_id, event if isinstance(event, str) else None)
            return {"unsubscribed": event}
        raise KeyError(method)

    @staticmethod
    def _valid_id(value: Any) -> bool:
        return value is None or (isinstance(value, (str, int, float)) and not isinstance(value, bool))

    @staticmethod
    def _error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    def _write(self, value: Mapping[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":"), ensure_ascii=True)
        if len(encoded.encode("utf-8")) > self._limits.max_frame_bytes:
            raise McpTransportError("MCP response exceeds max_frame_bytes")
        with self._write_lock:
            self._output.write(encoded + "\n")
            flush = getattr(self._output, "flush", None)
            if callable(flush):
                flush()

    def _on_notification(self, event: str, payload: Mapping[str, Any]) -> None:
        self._write({"jsonrpc": "2.0", "method": "notifications/event",
                     "params": {"event": event, "payload": dict(payload)}})


__all__ = ["McpTransportError", "McpTransportLimits", "StdioMcpTransport"]
