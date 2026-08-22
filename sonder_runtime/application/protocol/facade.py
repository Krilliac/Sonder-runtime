"""Application-owned composition facade for the protocol production seam.

The facade is intentionally transport and provider neutral.  HTTP, MCP,
editor, mobile, and CLI handlers can share the same schema, event vocabulary,
OpenAI compatibility, and reconnect policy without reaching into one another
or into persistence.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping

from ..ports.protocol import ProtocolAuthorization
from .client_schema import ClientParityContract, ClientSchema, ReconnectRequest, ReconnectResponse, build_client_schema
from .events import ProtocolEventType, event_name
from .mcp_compatibility import McpCompatibility
from .openai_compatibility import OpenAICompatibility
from .resumable_streams import ResumableStream


class ProtocolAuthorizationError(PermissionError):
    """Raised when a protocol operation is not authorized."""


@dataclass(frozen=True, slots=True)
class ProtocolGraph:
    schema: ClientSchema
    streams: Mapping[str, ResumableStream]
    openai: OpenAICompatibility
    mcp: McpCompatibility


class _DenyByDefault:
    def authorize(self, operation: str, client_id: str) -> bool:
        del operation, client_id
        return False


class ProtocolApplicationFacade:
    """Typed facade shared by all protocol-facing application adapters."""

    def __init__(
        self,
        graph: ProtocolGraph,
        *,
        authorization: ProtocolAuthorization | None = None,
        event_hook: Callable[[Mapping[str, Any]], Any] | None = None,
    ) -> None:
        if not isinstance(graph, ProtocolGraph):
            raise TypeError("graph must be a ProtocolGraph")
        if authorization is not None and not callable(getattr(authorization, "authorize", None)):
            raise TypeError("authorization must expose authorize")
        self._graph = graph
        self._authorization = authorization or _DenyByDefault()
        self._event_hook = event_hook

    @property
    def graph(self) -> ProtocolGraph:
        return self._graph

    @property
    def schema(self) -> ClientSchema:
        return self._graph.schema

    @property
    def openai(self) -> OpenAICompatibility:
        return self._graph.openai

    @property
    def mcp(self) -> McpCompatibility:
        return self._graph.mcp

    def publish(self, stream_id: str, event_type: ProtocolEventType | str,
                payload: dict, *, event_id: str):
        """Publish one bounded event and report it through the shared hook."""
        name = event_name(event_type)
        try:
            stream = self._graph.streams[stream_id]
        except KeyError as exc:
            raise ValueError("unknown protocol stream") from exc
        event = stream.publish(name, payload, event_id=event_id)
        self._emit({"kind": name, "stream_id": stream_id, "sequence": event.sequence,
                    "event_id": event.event_id})
        return event

    def open_stream(
        self, stream_id: str, *, client_id: str, capacity: int = 256,
    ) -> ResumableStream:
        """Open one bounded reconnectable stream for an authorized host.

        Stream creation is an explicit application operation.  The default
        authorization policy denies it, and the facade never creates a stream
        merely because a client presents a reconnect cursor.
        """
        if not isinstance(client_id, str) or not client_id.strip():
            raise ProtocolAuthorizationError("client identity is required")
        if self._authorization.authorize("protocol.stream.create", client_id) is not True:
            raise ProtocolAuthorizationError("protocol stream creation is not authorized")
        streams = self._graph.streams
        if stream_id in streams:
            raise ValueError("protocol stream already exists")
        if not isinstance(streams, dict):
            raise ValueError("protocol stream registry is not mutable")
        stream = ResumableStream(stream_id, capacity=capacity)
        streams[stream_id] = stream
        self._emit({"kind": "stream.opened", "stream_id": stream_id,
                    "client_id": client_id, "capacity": capacity})
        return stream

    def reconnect(self, request: ReconnectRequest, *, client_id: str | None = None) -> ReconnectResponse:
        """Authorize and execute one bounded reconnect plan."""
        if not isinstance(request, ReconnectRequest):
            raise TypeError("request must be a ReconnectRequest")
        principal = client_id or request.client_id
        if principal != request.client_id:
            raise ProtocolAuthorizationError("client identity does not match reconnect request")
        if self._authorization.authorize("protocol.reconnect", principal) is not True:
            raise ProtocolAuthorizationError("protocol reconnect is not authorized")
        response = ClientParityContract(self.schema, self._graph.streams).reconnect(request)
        self._emit({"kind": ProtocolEventType.CONNECTION_RECONNECTED.value,
                    "client_id": principal, "resumed": response.resumed})
        return response

    def _emit(self, event: Mapping[str, Any]) -> None:
        if self._event_hook is not None:
            self._event_hook(dict(event))

    @classmethod
    def compose(
        cls,
        catalogs: Any,
        *,
        streams: Mapping[str, ResumableStream] | None = None,
        authorization: ProtocolAuthorization | None = None,
        event_hook: Callable[[Mapping[str, Any]], Any] | None = None,
        mcp: McpCompatibility | None = None,
        openai: OpenAICompatibility | None = None,
    ) -> "ProtocolApplicationFacade":
        materialized = dict(streams or {})
        if any(key != stream.stream_id for key, stream in materialized.items()):
            raise ValueError("stream mapping keys must match stream ids")
        schema = build_client_schema(catalogs)
        return cls(
            ProtocolGraph(schema, materialized, openai or OpenAICompatibility(),
                          mcp or McpCompatibility()),
            authorization=authorization, event_hook=event_hook,
        )


__all__ = ["ProtocolApplicationFacade", "ProtocolAuthorizationError", "ProtocolGraph"]
