"""Bounded, export-neutral projection for local agent observations.

The input is the already-sanitized ``LocalObservabilitySink.recent_events``
shape.  This module deliberately does not import an OpenTelemetry SDK or emit
anything.  It provides a stable mapping target for a future host adapter while
keeping content, prompts, arguments, commands, and model output out of the
projection by construction.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Iterable

from ...domain.common.errors import InvalidInput


MAX_TRACE_SPANS = 256
_ERROR_LEVELS = frozenset(("ERROR", "CRITICAL"))


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value, field: str, *, required: bool = True) -> str:
    if type(value) is not str or (required and not value):
        raise InvalidInput(f"trace event field {field!r} is invalid")
    return value


def _operation(event_code: str, category: str) -> str:
    lowered = event_code.lower()
    if "tool" in lowered:
        return "execute_tool"
    if "model" in lowered or "inference" in lowered:
        return "invoke_agent"
    if "plan" in lowered:
        return "plan"
    if "workflow" in lowered or category == "workflow":
        return "invoke_workflow"
    return "runtime_event"


@dataclass(frozen=True, slots=True)
class TraceSpan:
    """A safe span-like record with no event detail payload."""

    trace_id: str
    span_id: str
    name: str
    operation: str
    category: str
    event_code: str
    severity: str
    sequence: int
    observed_at_monotonic: float
    duration_ms: int | None
    status: str
    operation_id: str | None = None

    def to_dict(self) -> dict[str, object]:
        attributes = {
            "gen_ai.operation.name": self.operation,
            "sonder.category": self.category,
            "sonder.event_code": self.event_code,
            "sonder.severity": self.severity,
            "sonder.sequence": self.sequence,
        }
        if self.operation_id is not None:
            attributes["sonder.operation_id"] = self.operation_id
        row: dict[str, object] = {
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "name": self.name,
            "operation": self.operation,
            "kind": "internal",
            "sequence": self.sequence,
            "observed_at_monotonic": self.observed_at_monotonic,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "attributes": attributes,
            "redacted": True,
        }
        return row


@dataclass(frozen=True, slots=True)
class TraceExport:
    """Bounded trace projection; it is not an exporter or delivery receipt."""

    spans: tuple[TraceSpan, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "sonder.trace-span.v1",
            "spans": [span.to_dict() for span in self.spans],
            "span_count": len(self.spans),
            "redacted": True,
            "export": "disabled",
            "time_semantics": "monotonic_observation",
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def project_trace(
    events: Iterable[dict], *, max_spans: int = MAX_TRACE_SPANS
) -> TraceExport:
    """Project sanitized local events into bounded, OTel-shaped span records.

    Events are ordered oldest-first so repeated snapshots are deterministic.
    Raw ``fields`` are ignored even when present; callers must provide the
    local-observability projection rather than an authoritative raw event.
    """
    if not 1 <= max_spans <= MAX_TRACE_SPANS:
        raise InvalidInput("max_spans is out of bounds")
    rows = tuple(events)
    if len(rows) > max_spans:
        raise InvalidInput("trace exceeds max_spans")
    parsed: list[tuple[int, dict]] = []
    for row in rows:
        if type(row) is not dict:
            raise InvalidInput("trace requires event dictionaries")
        sequence = row.get("sequence")
        if type(sequence) is not int or isinstance(sequence, bool) or sequence < 1:
            raise InvalidInput("trace event sequence is invalid")
        parsed.append((sequence, row))
    parsed.sort(key=lambda item: item[0])
    spans: list[TraceSpan] = []
    for sequence, row in parsed:
        correlation = _text(row.get("correlation_id"), "correlation_id")
        category = _text(row.get("category"), "category")
        event_code = _text(row.get("event_code"), "event_code")
        severity = _text(row.get("severity"), "severity").upper()
        observed = row.get("observed_at")
        if type(observed) not in (int, float) or isinstance(observed, bool):
            raise InvalidInput("trace event observed_at is invalid")
        duration = row.get("duration_ms")
        if duration is not None and (
            type(duration) not in (int, float) or isinstance(duration, bool)
        ):
            raise InvalidInput("trace event duration_ms is invalid")
        duration_ms = None if duration is None else max(0, int(duration))
        operation = _operation(event_code, category)
        trace_id = _digest(correlation)[:32]
        span_id = _digest(f"{correlation}\0{sequence}")[:16]
        status = "error" if severity in _ERROR_LEVELS else "ok"
        operation_id = row.get("operation_id")
        if operation_id is not None and type(operation_id) is not str:
            raise InvalidInput("trace event operation_id is invalid")
        spans.append(TraceSpan(
            trace_id, span_id, f"{operation} {event_code}", operation,
            category, event_code, severity, sequence, float(observed),
            duration_ms, status, operation_id,
        ))
    return TraceExport(tuple(spans))


__all__ = ["MAX_TRACE_SPANS", "TraceExport", "TraceSpan", "project_trace"]
