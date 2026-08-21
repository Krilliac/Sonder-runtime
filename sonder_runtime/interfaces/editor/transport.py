"""Bounded newline-delimited transport for the editor interchange envelope.

This is deliberately provider-neutral.  It carries the existing typed
``ProtocolEnvelope`` contract over an injectable stream and owns only framing,
handshake, cancellation validation, and response correlation.  Filesystem,
tool, and editor operations remain application callbacks.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Mapping, TextIO

from ...application.protocol.editor_interop import (
    CancellationRequest,
    EditorInteropError,
    ImplementationInfo,
    ProtocolEnvelope,
)


@dataclass(frozen=True)
class EditorTransportLimits:
    max_frame_bytes: int = 256_000
    max_messages: int = 256

    def __post_init__(self) -> None:
        if any(
            isinstance(value, bool) or not isinstance(value, int) or value <= 0
            for value in (self.max_frame_bytes, self.max_messages)
        ):
            raise ValueError("editor transport limits must be positive integers")


class EditorTransportError(ValueError):
    """A bounded editor transport or envelope violation."""


EditorRequestHandler = Callable[[ProtocolEnvelope], Mapping[str, Any] | None]
CancellationHandler = Callable[[CancellationRequest], None]


class EditorStdioTransport:
    """Serve versioned editor envelopes over newline-delimited text streams."""

    def __init__(
        self,
        input_stream: TextIO,
        output_stream: TextIO,
        *,
        server: ImplementationInfo,
        handler: EditorRequestHandler | None = None,
        cancellation_handler: CancellationHandler | None = None,
        limits: EditorTransportLimits | None = None,
    ) -> None:
        if not callable(getattr(input_stream, "readline", None)):
            raise ValueError("editor transport requires a readable input stream")
        if not callable(getattr(output_stream, "write", None)):
            raise ValueError("editor transport requires a writable output stream")
        if not isinstance(server, ImplementationInfo):
            raise TypeError("server must be ImplementationInfo")
        self._input = input_stream
        self._output = output_stream
        self._server = server
        self._handler = handler
        self._cancellation_handler = cancellation_handler
        self._limits = limits or EditorTransportLimits()
        self._initialized = False
        self._client: ImplementationInfo | None = None
        self._negotiated_capabilities: frozenset[str] = frozenset()

    @property
    def initialized(self) -> bool:
        return self._initialized

    @property
    def client_implementation(self) -> ImplementationInfo | None:
        return self._client

    @property
    def negotiated_capabilities(self) -> frozenset[str]:
        return self._negotiated_capabilities

    def serve(self) -> int:
        """Process bounded frames until EOF and return the frame count."""
        count = 0
        while count < self._limits.max_messages:
            raw = self._input.readline(self._limits.max_frame_bytes + 1)
            if raw in ("", b""):
                break
            if raw.strip() == "" or raw.strip() == b"":
                continue
            count += 1
            try:
                response = self._dispatch(self._decode(raw))
            except EditorTransportError as exc:
                response = self._error(None, str(exc))
            if response is not None:
                self._write(response)
        if count >= self._limits.max_messages:
            self._write(self._error(None, "editor message limit exceeded"))
        return count

    def _decode(self, raw: str | bytes) -> ProtocolEnvelope:
        if isinstance(raw, bytes):
            if len(raw) > self._limits.max_frame_bytes:
                raise EditorTransportError("editor frame exceeds max_frame_bytes")
            try:
                raw = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise EditorTransportError("editor frame is not UTF-8") from exc
        if len(raw.encode("utf-8")) > self._limits.max_frame_bytes:
            raise EditorTransportError("editor frame exceeds max_frame_bytes")
        try:
            value = json.loads(raw)
            return ProtocolEnvelope.from_dict(value)
        except (TypeError, ValueError, json.JSONDecodeError, EditorInteropError) as exc:
            raise EditorTransportError("invalid editor envelope") from exc

    def _dispatch(self, envelope: ProtocolEnvelope) -> dict[str, Any] | None:
        if not self._initialized:
            if envelope.message_type != "session/initialize":
                raise EditorTransportError("session/initialize is required")
            implementation = envelope.payload.get("implementation")
            if implementation is not None:
                try:
                    self._client = ImplementationInfo.from_dict(implementation)
                except (EditorInteropError, TypeError, ValueError) as exc:
                    raise EditorTransportError("invalid client implementation metadata") from exc
                self._negotiated_capabilities = self._server.negotiate(self._client)
            self._initialized = True
            return self._response(
                envelope,
                "session/initialized",
                {
                    "implementation": self._server.to_dict(),
                    "capabilities": sorted(self._negotiated_capabilities),
                },
            )
        if envelope.message_type == "session/initialize":
            raise EditorTransportError("session/initialize may only be sent once")
        if envelope.message_type == "session/cancel_request":
            try:
                request = CancellationRequest(
                    str(envelope.payload["request_id"]),
                    str(envelope.payload["session_id"]),
                    str(envelope.payload.get("reason", "cancelled")),
                )
            except (KeyError, EditorInteropError, TypeError, ValueError) as exc:
                raise EditorTransportError("invalid cancellation request") from exc
            if self._cancellation_handler is not None:
                self._cancellation_handler(request)
            return self._response(
                envelope,
                "session/cancelled",
                {"request_id": request.request_id, "session_id": request.session_id},
            )
        if self._handler is None:
            raise EditorTransportError("editor request handler is unavailable")
        try:
            result = self._handler(envelope)
        except Exception as exc:
            raise EditorTransportError(
                f"editor request failed: {type(exc).__name__}"
            ) from exc
        if result is None:
            return None
        if not isinstance(result, Mapping):
            raise EditorTransportError("editor handler must return a mapping")
        return self._response(envelope, f"{envelope.message_type}/result", result)

    @staticmethod
    def _response(
        request: ProtocolEnvelope, message_type: str, payload: Mapping[str, Any]
    ) -> dict[str, Any]:
        return ProtocolEnvelope(
            message_type,
            {"request_id": request.message_id, **dict(payload)},
            request.message_id,
        ).to_dict()

    @staticmethod
    def _error(request_id: str | None, message: str) -> dict[str, Any]:
        return ProtocolEnvelope.create(
            "error", {"request_id": request_id, "message": message}
        ).to_dict()

    def _write(self, value: Mapping[str, Any]) -> None:
        encoded = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > self._limits.max_frame_bytes:
            raise EditorTransportError("editor response exceeds max_frame_bytes")
        self._output.write(encoded + "\n")
        flush = getattr(self._output, "flush", None)
        if callable(flush):
            flush()


__all__ = ["EditorStdioTransport", "EditorTransportError", "EditorTransportLimits"]
