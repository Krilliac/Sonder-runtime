"""Bounded JSON-RPC transport for the provider-neutral MCP contracts."""
from __future__ import annotations

import json
import logging
import threading
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, TextIO

logger = logging.getLogger(__name__)

from ...application.protocol.mcp_compatibility import (
    LegacyMcpContract, McpCompatibility, McpNegotiation, McpNegotiationError,
    SubscriptionNotificationRouter,
)
from ...application.protocol.mcp_tasks import McpTaskInvalidInput, McpTaskNotFound
from ...application.tools.generated_catalogs import GeneratedCatalogs
from ...application.ports.tool_registry import ToolDescriptor


@dataclass(frozen=True)
class McpTransportLimits:
    max_frame_bytes: int = 256_000
    max_tools: int = 256
    max_arguments_bytes: int = 64_000
    max_exchange_bytes: int = 1_024_000

    def __post_init__(self) -> None:
        if any(isinstance(v, bool) or v <= 0 for v in (
            self.max_frame_bytes, self.max_tools, self.max_arguments_bytes,
            self.max_exchange_bytes,
        )):
            raise ValueError("MCP transport limits must be positive integers")


class McpTransportError(ValueError):
    """A bounded transport or protocol violation."""


class McpProvider(Protocol):
    """Provider-neutral one-shot MCP exchange port.

    Implementations own their process/socket lifecycle.  The interface only
    carries the newline-delimited MCP bytes as text, so stream and subprocess
    providers share the same bounded exchange path without importing one
    another.
    """

    def run(self, request: str) -> tuple[str, str]:
        """Exchange a complete request stream and return stdout/stderr."""


class BoundedMcpProviderExchange:
    """Adapt any one-shot provider to the bounded MCP exchange boundary.

    The exchange deliberately does not decode, reorder, or filter JSON-RPC
    messages.  This preserves negotiated notifications and lets providers
    use the same stream semantics as :class:`StdioMcpTransport`.
    """

    def __init__(self, provider: McpProvider, *, limits: McpTransportLimits | None = None) -> None:
        if not callable(getattr(provider, "run", None)):
            raise ValueError("MCP provider must expose a callable run method")
        self._provider = provider
        self._limits = limits or McpTransportLimits()

    def exchange(self, request: str) -> tuple[str, str]:
        if not isinstance(request, str):
            raise TypeError("MCP provider request must be text")
        request_bytes = len(request.encode("utf-8"))
        logger.debug(f"provider exchange request_bytes={request_bytes}")
        exchange_ratio = request_bytes / self._limits.max_exchange_bytes if self._limits.max_exchange_bytes else 1.0
        if exchange_ratio >= 0.8 and request_bytes <= self._limits.max_exchange_bytes:
            logger.warning(
                f"MCP provider request approaching size limit: "
                f"{request_bytes}/{self._limits.max_exchange_bytes} bytes ({exchange_ratio:.0%})"
            )
        if request_bytes > self._limits.max_exchange_bytes:
            raise McpTransportError("MCP request exceeds max_exchange_bytes")
        stdout, stderr = self._provider.run(request)
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise McpTransportError("MCP provider must return text stdout and stderr")
        response_bytes = len(stdout.encode("utf-8"))
        resp_ratio = response_bytes / self._limits.max_exchange_bytes if self._limits.max_exchange_bytes else 1.0
        if resp_ratio >= 0.8 and response_bytes <= self._limits.max_exchange_bytes:
            logger.warning(
                f"MCP provider response approaching size limit: "
                f"{response_bytes}/{self._limits.max_exchange_bytes} bytes ({resp_ratio:.0%})"
            )
        if response_bytes > self._limits.max_exchange_bytes:
            raise McpTransportError("MCP response exceeds max_exchange_bytes")
        logger.debug(f"provider exchange completed response_bytes={response_bytes}")
        return stdout, stderr


class StdioMcpTransport:
    """Serve newline-delimited JSON-RPC over injectable text-like streams."""

    def __init__(
        self, input_stream: TextIO, output_stream: TextIO, *,
        compatibility: McpCompatibility, tool_catalog: Any = (), tool_handler: Any,
        task_handler: Any = None,
        legacy_contract: LegacyMcpContract | None = None,
        notifications: SubscriptionNotificationRouter | None = None,
        connection_id: str = "stdio", limits: McpTransportLimits | None = None,
    ) -> None:
        if not connection_id or not callable(getattr(input_stream, "readline", None)):
            raise ValueError("MCP transport requires an input stream and connection id")
        if not callable(getattr(output_stream, "write", None)):
            raise ValueError("MCP transport requires a writable output stream")
        if legacy_contract is not None and legacy_contract not in compatibility.legacy_contracts:
            raise McpTransportError(
                "legacy MCP transport declaration is not registered with compatibility"
            )
        self._input, self._output = input_stream, output_stream
        self._compatibility, self._handler = compatibility, tool_handler
        self._task_handler = task_handler
        self._legacy_contract = legacy_contract
        self._router, self._connection_id = notifications, connection_id
        self._limits = limits or McpTransportLimits()
        self._negotiation: McpNegotiation | None = None
        self._write_lock = threading.Lock()
        self._catalog = self._build_catalog(tool_catalog)
        if len(self._catalog) > self._limits.max_tools:
            raise McpTransportError("tool catalog exceeds max_tools")
        catalog_ratio = len(self._catalog) / self._limits.max_tools if self._limits.max_tools else 1.0
        if catalog_ratio >= 0.8:
            logger.warning(
                f"tool catalog approaching limit: {len(self._catalog)}/{self._limits.max_tools} "
                f"tools ({catalog_ratio:.0%}), connection_id={self._connection_id!r}"
            )
        logger.debug(
            f"StdioMcpTransport initialized connection_id={self._connection_id!r} "
            f"tools={len(self._catalog)}"
        )
        logger.info(f"MCP transport ready connection_id={self._connection_id!r}, tools={len(self._catalog)}")

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
        logger.info(f"MCP serve loop started connection_id={self._connection_id!r}")
        logger.debug(f"serve loop starting connection_id={self._connection_id!r}")
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
                logger.error(
                    f"MCP transport protocol error connection_id={self._connection_id!r}: {exc}",
                    exc_info=True,
                )
                response = self._error(None, -32700, str(exc))
            if response is not None:
                self._write(response)
        if self._router is not None:
            self._router.unsubscribe(self._connection_id)
        logger.debug(f"serve loop ended connection_id={self._connection_id!r} frames_processed={count}")
        logger.info(f"MCP serve loop ended connection_id={self._connection_id!r}, frames_processed={count}")
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
        logger.debug(f"dispatch method={request.get('method')!r} notification={notification}")
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
        except McpTaskInvalidInput as exc:
            return self._error(request_id, -32602, str(exc))
        except McpTaskNotFound as exc:
            return self._error(request_id, -32601, str(exc))
        except KeyError as exc:
            return self._error(request_id, -32601, str(exc))
        except Exception:
            logger.error(
                f"unhandled exception in MCP handler method={method!r}, "
                f"connection_id={self._connection_id!r}",
                exc_info=True,
            )
            logger.warning(
                f"internal MCP handler error on method={method!r}, "
                f"connection_id={self._connection_id!r}",
                exc_info=True,
            )
            return self._error(request_id, -32603, "internal MCP handler error")
        if notification:
            return None
        return {"jsonrpc": "2.0", "id": request_id, "result": result}

    def _call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        logger.debug(f"_call method={method!r}")
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
                tuple(versions),
                client_capabilities=tuple(str(k) for k in capabilities),
                legacy_contract=self._legacy_contract,
            )
            logger.debug(
                f"MCP negotiation agreed version={self._negotiation.agreed_version!r} "
                f"capabilities={sorted(self._negotiation.capabilities)}"
            )
            logger.info(
                f"MCP session negotiated version={self._negotiation.agreed_version!r}, "
                f"capabilities={sorted(self._negotiation.capabilities)}"
            )
            return {"protocolVersion": self._negotiation.agreed_version,
                    "capabilities": {name: {} for name in self._negotiation.capabilities},
                    "serverInfo": {"name": "sonder-runtime", "version": self._negotiation.server_version}}
        if self._negotiation is None:
            raise McpTransportError("initialize is required")
        if method == "notifications/initialized":
            return {}
        if method in {"tasks/get", "tasks/cancel", "tasks/update"}:
            if "tasks" not in (self._negotiation.capabilities if self._negotiation else ()):
                raise McpTransportError("MCP Tasks capability was not negotiated")
            if not callable(self._task_handler):
                raise McpTransportError("MCP Tasks handler is not configured")
            result = self._task_handler(method, params)
            if not isinstance(result, Mapping):
                raise McpTransportError("MCP Tasks handler returned an invalid result")
            return dict(result)
        if method == "tools/list":
            return {"tools": list(self._catalog)}
        if method == "tools/call":
            name, arguments = params.get("name"), params.get("arguments", {})
            logger.debug(f"tools/call name={name!r}")
            if not isinstance(name, str) or not name:
                raise McpTransportError("tools/call requires name")
            if not isinstance(arguments, dict):
                raise McpTransportError("tools/call arguments must be an object")
            if len(json.dumps(arguments, separators=(",", ":")).encode("utf-8")) > self._limits.max_arguments_bytes:
                raise McpTransportError("tool arguments exceed max_arguments_bytes")
            value = self._handler(name, arguments) if callable(self._handler) else self._handler.handle(name, arguments)
            if self._negotiation is not None and self._negotiation.agreed_version != "2.0":
                return self._standard_tool_result(value)
            return dict(value) if isinstance(value, Mapping) else {"output": value}
        if method == "sonder/subscribe":
            event = params.get("event")
            logger.debug(f"sonder/subscribe event={event!r} connection_id={self._connection_id!r}")
            if self._router is None or not isinstance(event, str) or not event:
                raise McpTransportError("subscription router and event are required")
            self._router.subscribe(self._connection_id, event, self._on_notification)
            return {"subscribed": event}
        if method == "sonder/unsubscribe":
            event = params.get("event")
            logger.debug(f"sonder/unsubscribe event={event!r} connection_id={self._connection_id!r}")
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

    @staticmethod
    def _standard_tool_result(value: Any) -> dict[str, Any]:
        """Render standard MCP CallToolResult without changing legacy 2.0."""
        if isinstance(value, Mapping) and "content" in value:
            return dict(value)
        structured = dict(value) if isinstance(value, Mapping) else {"value": value}
        return {
            "content": [{
                "type": "text",
                "text": json.dumps(structured, ensure_ascii=False, separators=(",", ":")),
            }],
            "structuredContent": structured,
        }

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
        logger.debug(f"sending notification event={event!r} connection_id={self._connection_id!r}")
        self._write({"jsonrpc": "2.0", "method": "notifications/event",
                     "params": {"event": event, "payload": dict(payload)}})


__all__ = ["McpTransportError", "McpTransportLimits", "StdioMcpTransport"]
