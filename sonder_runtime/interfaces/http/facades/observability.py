"""Read-only HTTP presentation for sanitized local observability."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Sequence

from ....application.observability.trace_projection import MAX_TRACE_SPANS

# Bounds the raw query value before it is handed to the sink's equality
# filter. This is a transport-layer sanity bound, not the source of truth for
# what a valid correlation/category/severity value looks like -- the sink
# already only ever retains identifier-shaped values, so an oversized or
# malformed filter simply matches nothing.
_MAX_FILTER_VALUE = 96
_FILTER_PARAMS = ("correlation_id", "category", "severity")


@dataclass(frozen=True, slots=True)
class TraceHttpResult:
    body: dict[str, object]
    status_code: int = 200


def _single_value(
    query: Mapping[str, Sequence[str]], name: str, default: str
) -> tuple[str | None, TraceHttpResult | None]:
    values = query.get(name, (default,))
    if len(values) != 1:
        return None, TraceHttpResult(
            {"error": {"message": f"{name} must be supplied once", "type": "invalid_request"}},
            400,
        )
    value = values[0]
    if len(value) > _MAX_FILTER_VALUE:
        return None, TraceHttpResult(
            {"error": {"message": f"{name} exceeds the maximum length", "type": "invalid_request"}},
            400,
        )
    return value, None


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
    bounded_query = query or {}
    limit_value, error = _single_value(bounded_query, "limit", str(MAX_TRACE_SPANS))
    if error is not None:
        return error
    try:
        limit = int(limit_value)
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
    filters: dict[str, str] = {}
    for name in _FILTER_PARAMS:
        value, error = _single_value(bounded_query, name, "")
        if error is not None:
            return error
        filters[name] = value
    projector = getattr(events, "trace_projection", None)
    if not callable(projector):
        return TraceHttpResult(
            {"error": {"message": "observability projection is unavailable", "type": "server_error"}},
            503,
        )
    try:
        projection = projector(limit=limit, **filters)
        body = projection.to_dict()
    except Exception:
        return TraceHttpResult(
            {"error": {"message": "observability projection failed", "type": "server_error"}},
            500,
        )
    body["object"] = "trace_projection"
    return TraceHttpResult(body)


__all__ = ["TraceHttpResult", "dispatch_trace_route"]
