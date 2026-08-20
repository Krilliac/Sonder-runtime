"""Provider-neutral JSON wire contract for intermittent mobile clients.

This codec is intentionally boring: it validates and serializes the typed
application contract, but owns no socket, HTTP, Flutter, or provider code.
The same payloads can therefore be used by Flutter, desktop, web, and CLI
adapters while stream history remains owned by :class:`ClientParityContract`.
"""
from __future__ import annotations

from typing import Any, Mapping

from .client_schema import (
    ClientSchema,
    FreshnessResult,
    ReconnectRequest,
    ReconnectResponse,
    ResumeCursor,
    ResumeDisposition,
    ResumeResult,
    SchemaFreshness,
    _require_digest,
)
from ...domain.protocol.events import EventEnvelope, Snapshot


class MobileWireError(ValueError):
    """Raised for malformed or unsupported reconnect payloads."""


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise MobileWireError(f"{label} must be an object")
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], label: str) -> None:
    unknown = set(value) - allowed
    if unknown:
        raise MobileWireError(f"{label} contains unsupported field(s): {', '.join(sorted(unknown))}")


def encode_reconnect_request(request: ReconnectRequest) -> dict[str, Any]:
    if not isinstance(request, ReconnectRequest):
        raise TypeError("request must be a ReconnectRequest")
    return {
        "batch_limit": request.batch_limit,
        "client_id": request.client_id,
        "cursors": [
            {"stream_id": cursor.stream_id, "watermark": cursor.watermark}
            for cursor in request.cursors
        ],
        "schema_digest": request.schema_digest,
        "type": "reconnect",
        "version": 1,
    }


def decode_reconnect_request(payload: Mapping[str, Any]) -> ReconnectRequest:
    value = _object(payload, "reconnect request")
    _keys(value, {"batch_limit", "client_id", "cursors", "schema_digest", "type", "version"}, "reconnect request")
    if value.get("type") != "reconnect" or value.get("version") != 1:
        raise MobileWireError("unsupported reconnect request version")
    raw_cursors = value.get("cursors", ())
    if not isinstance(raw_cursors, (list, tuple)):
        raise MobileWireError("cursors must be an array")
    cursors = []
    for raw in raw_cursors:
        item = _object(raw, "resume cursor")
        _keys(item, {"stream_id", "watermark"}, "resume cursor")
        try:
            cursors.append(ResumeCursor(str(item["stream_id"]), item["watermark"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise MobileWireError("invalid resume cursor") from exc
    digest = value.get("schema_digest")
    if digest is not None:
        try:
            digest = _require_digest(digest)
        except ValueError as exc:
            raise MobileWireError("invalid schema_digest") from exc
    try:
        return ReconnectRequest(
            client_id=value["client_id"], schema_digest=digest,
            cursors=tuple(cursors), batch_limit=value.get("batch_limit", 256),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise MobileWireError("invalid reconnect request") from exc


def _encode_batch(batch: Any) -> dict[str, Any]:
    if batch is None:
        return {}
    snapshot = None
    if batch.snapshot is not None:
        snapshot = {
            "state": dict(batch.snapshot.state),
            "stream_id": batch.snapshot.stream_id,
            "watermark": batch.snapshot.watermark,
        }
    return {
        "events": [
            {
                "event_id": event.event_id,
                "event_type": event.event_type,
                "kind": event.kind.value,
                "payload": dict(event.payload),
                "sequence": event.sequence,
                "stream_id": event.stream_id,
            } for event in batch.events
        ],
        "has_more": batch.has_more,
        "next_watermark": batch.next_watermark,
        "snapshot": snapshot,
    }


def encode_reconnect_response(response: ReconnectResponse) -> dict[str, Any]:
    if not isinstance(response, ReconnectResponse):
        raise TypeError("response must be a ReconnectResponse")
    return {
        "freshness": {
            "advertised_digest": response.freshness.advertised_digest,
            "expected_digest": response.freshness.expected_digest,
            "reason": response.freshness.reason,
            "state": response.freshness.state.value,
        },
        "results": [
            {
                "batch": _encode_batch(result.batch),
                "disposition": result.disposition.value,
                "reason": result.reason,
                "stream_id": result.stream_id,
            } for result in response.results
        ],
        "type": "reconnect_response",
        "version": 1,
    }


def encode_client_schema(schema: ClientSchema) -> dict[str, Any]:
    """Return the exact schema envelope a client may cache and advertise."""
    if not isinstance(schema, ClientSchema):
        raise TypeError("schema must be a ClientSchema")
    return {"type": "client_schema", "version": 1, "schema": schema.as_dict()}


__all__ = [
    "MobileWireError", "decode_reconnect_request", "encode_client_schema",
    "encode_reconnect_request", "encode_reconnect_response",
]
