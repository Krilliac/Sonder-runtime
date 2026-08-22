"""Read-only HTTP presentation for sanitized local observability."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ....application.observability.trace_projection import MAX_TRACE_SPANS


@dataclass(frozen=True, slots=True)
class TraceHttpResult:
    body: dict[str, object]
    status_code: int = 200


def dispatch_trace_route(
    events: object,
    method: str,
    path: str,
    query: Mapping[str, Sequence[str]] | None = None,
) -> TraceHttpResult | None:
    """Project only the already-redacted event sink through one GET route."""
    if path != "/v1/observability/trace":
        return None
    if method != "GET":
        return TraceHttpResult(
            {"error": {"message": "method not allowed", "type": "invalid_request"}},
            405,
        )
    values = (query or {}).get("limit", (str(MAX_TRACE_SPANS),))
    if len(values) != 1:
        return TraceHttpResult(
            {"error": {"message": "limit must be supplied once", "type": "invalid_request"}},
            400,
        )
    try:
        limit = int(values[0])
    except (TypeError, ValueError):
        return TraceHttpResult(
            {"error": {"message": "limit must be an integer between 1 and 256", "type": "invalid_request"}},
            400,
        )
    if not 1 <= limit <= MAX_TRACE_SPANS:
        return TraceHttpResult(
            {"error": {"message": "limit must be an integer between 1 and 256", "type": "invalid_request"}},
            400,
        )
    projector = getattr(events, "trace_projection", None)
    if not callable(projector):
        return TraceHttpResult(
            {"error": {"message": "observability projection is unavailable", "type": "server_error"}},
            503,
        )
    try:
        projection = projector(limit=limit)
        body = projection.to_dict()
    except Exception:
        return TraceHttpResult(
            {"error": {"message": "observability projection failed", "type": "server_error"}},
            500,
        )
    body["object"] = "trace_projection"
    return TraceHttpResult(body)


__all__ = ["TraceHttpResult", "dispatch_trace_route"]
