"""Thin HTTP adapter for the opt-in memory replication receiver.

Application code owns authentication, wire decoding, and sink validation.  The
adapter only performs HTTP framing and maps the receiver's stable outcomes to
bounded JSON responses.
"""
from __future__ import annotations

import re
from typing import Any

from ...application.memory.replication import MemoryReplicationReceiver


MEMORY_REPLICATION_ROUTE = "/v1/memory/replication/batches"


def is_memory_replication_route(path: object) -> bool:
    """Return true only for the exact batch endpoint without a query string."""

    return path == MEMORY_REPLICATION_ROUTE


def _single_header(handler: Any, name: str, *, required: bool = False) -> str:
    values = handler.headers.get_all(name) or ()
    if len(values) > 1 or (required and not values):
        raise ValueError("memory replication request headers are invalid")
    return values[0] if values else ""


def _send(handler: Any, payload: object, *, status: int) -> None:
    handler._send_json_payload(payload, status=status, headers={"Cache-Control": "no-store"})


def handle_memory_replication(
    handler: Any,
    method: str,
    receiver: MemoryReplicationReceiver | None,
) -> bool:
    """Handle the opt-in peer route and leave all other routes untouched."""

    handler._memory_replication_request = is_memory_replication_route(
        getattr(handler, "path", "")
    )
    if not handler._memory_replication_request:
        return False
    if receiver is None:
        _send(handler, {"error": {"code": "MEMORY_REPLICATION_UNAVAILABLE"}}, status=503)
        return True
    if method == "OPTIONS":
        _send(handler, {}, status=204)
        return True
    if method != "POST":
        _send(handler, {"error": {"code": "METHOD_NOT_ALLOWED"}}, status=405)
        return True
    try:
        if _single_header(handler, "Origin"):
            _send(handler, {"error": {"code": "ORIGIN_FORBIDDEN"}}, status=403)
            return True
        authorization = _single_header(handler, "Authorization", required=True)
        content_type = _single_header(handler, "Content-Type", required=True)
        if content_type.lower() != "application/json":
            raise ValueError("memory replication content type is invalid")
        content_length = _single_header(handler, "Content-Length", required=True)
        if not re.fullmatch(r"[0-9]{1,20}", content_length):
            raise ValueError("memory replication content length is invalid")
        length = int(content_length)
        if length > receiver.max_body_bytes:
            raise ValueError("memory replication request exceeds the body bound")
        body = handler.rfile.read(length)
        handler._request_body_consumed = True
        if len(body) != length:
            raise ValueError("memory replication request body is incomplete")
        receipt = receiver.receive_bytes(authorization, body)
    except PermissionError as exc:
        _send(handler, {"error": {"code": "UNAUTHORIZED", "message": str(exc)}}, status=401)
        return True
    except ValueError as exc:
        _send(handler, {"error": {"code": "INVALID_REQUEST", "message": str(exc)}}, status=400)
        return True
    except Exception:
        _send(handler, {"error": {"code": "MEMORY_REPLICATION_UNAVAILABLE"}}, status=503)
        return True
    _send(
        handler,
        {
            "object": "memory_replication_receipt",
            "receipt": receipt.as_dict(),
        },
        status=202,
    )
    return True


__all__ = [
    "MEMORY_REPLICATION_ROUTE",
    "MemoryReplicationReceiver",
    "handle_memory_replication",
    "is_memory_replication_route",
]
