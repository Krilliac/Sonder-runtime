"""Bounded operations tracing and health contracts (WP9 OPS-001/003/006).

This module is deliberately provider-neutral.  It creates OpenTelemetry-shaped
records without importing an exporter, and applies privacy and cardinality
limits before a record can leave the application boundary.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from hashlib import sha256
from typing import Any, Mapping, Protocol

from ..context import OperationContext

MAX_LABELS = 16
MAX_LABEL_KEY = 48
MAX_LABEL_VALUE = 96
MAX_ATTRIBUTES = 24
MAX_ATTRIBUTE_VALUE = 256
MAX_RECORD_BYTES = 16_384
MAX_CARDINALITY = 32

_SENSITIVE_KEYS = frozenset({
    "prompt", "messages", "content", "memory", "secret", "token",
    "api_key", "authorization", "credential", "password", "artifact",
    "dataset", "raw_input", "raw_output", "augmented_prompt",
})


class TraceExporter(Protocol):
    def export(self, record: "TraceRecord") -> None: ...


def _safe_text(value: object, limit: int) -> str:
    text = str(value)
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _key(value: object) -> str:
    return _safe_text(value, MAX_LABEL_KEY).strip().replace(" ", "_") or "unknown"


def _digest(value: object) -> str:
    return "sha256:" + sha256(str(value).encode("utf-8", "replace")).hexdigest()[:16]


def _redact(value: object, *, allow_content: bool = False, depth: int = 0) -> object:
    if depth > 3:
        return "[depth-limited]"
    if isinstance(value, Mapping):
        output: dict[str, object] = {}
        for raw_key, child in list(value.items())[:MAX_ATTRIBUTES]:
            name = _key(raw_key)
            if name.casefold() in _SENSITIVE_KEYS and not allow_content:
                output[name] = "[redacted]"
            else:
                output[name] = _redact(child, allow_content=allow_content, depth=depth + 1)
        return output
    if isinstance(value, (list, tuple, set)):
        return [_redact(item, allow_content=allow_content, depth=depth + 1) for item in list(value)[:8]]
    if isinstance(value, str):
        return _safe_text(value, MAX_ATTRIBUTE_VALUE)
    if isinstance(value, (bool, int, float)) or value is None:
        return value
    return _safe_text(value, MAX_ATTRIBUTE_VALUE)


class CardinalityLimiter:
    """Bound distinct label values per key and collapse excess values."""

    def __init__(self, maximum: int = MAX_CARDINALITY) -> None:
        if maximum < 1:
            raise ValueError("maximum must be positive")
        self._maximum = maximum
        self._seen: dict[str, set[str]] = {}

    def label(self, name: object, value: object) -> tuple[str, str]:
        label_name = _key(name)
        label_value = _safe_text(value, MAX_LABEL_VALUE)
        seen = self._seen.setdefault(label_name, set())
        if label_value not in seen and len(seen) >= self._maximum:
            label_value = "__cardinality_overflow__"
        seen.add(label_value)
        return label_name, label_value


@dataclass(frozen=True, slots=True)
class TraceRecord:
    """A bounded, already-redacted span/event envelope."""

    trace_id: str
    span_id: str
    operation: str
    status: str
    started_at: datetime
    duration_ms: float = 0.0
    parent_span_id: str | None = None
    labels: Mapping[str, str] = field(default_factory=dict)
    attributes: Mapping[str, object] = field(default_factory=dict)
    redaction_applied: bool = True

    def __post_init__(self) -> None:
        if not self.trace_id or not self.span_id or not self.operation:
            raise ValueError("trace_id, span_id, and operation are required")
        if self.status not in {"ok", "error", "unset"}:
            raise ValueError("status must be ok, error, or unset")
        if self.duration_ms < 0 or self.duration_ms != self.duration_ms:
            raise ValueError("duration_ms must be finite and non-negative")
        if not self.redaction_applied:
            raise ValueError("records must be redacted before export")
        object.__setattr__(self, "labels", dict(list(self.labels.items())[:MAX_LABELS]))
        object.__setattr__(self, "attributes", dict(list(self.attributes.items())[:MAX_ATTRIBUTES]))

    def export_size(self) -> int:
        return len(repr(self).encode("utf-8", "replace"))


class BoundedTracer:
    """Build and export records after redaction, label and size enforcement."""

    def __init__(self, exporter: TraceExporter, *, allow_content: bool = False,
                 cardinality: CardinalityLimiter | None = None) -> None:
        self._exporter = exporter
        self._allow_content = allow_content
        self._cardinality = cardinality or CardinalityLimiter()

    def emit(self, context: OperationContext, *, operation: str, status: str = "unset",
             duration_ms: float = 0.0, span_id: str = "root", parent_span_id: str | None = None,
             labels: Mapping[str, object] | None = None,
             attributes: Mapping[str, object] | None = None) -> TraceRecord:
        bounded_labels: dict[str, str] = {}
        for name, value in list((labels or {}).items())[:MAX_LABELS]:
            label_name, label_value = self._cardinality.label(name, value)
            bounded_labels[label_name] = label_value
        safe_attributes = _redact(dict(list((attributes or {}).items())[:MAX_ATTRIBUTES]), allow_content=self._allow_content)
        record = TraceRecord(
            trace_id=_safe_text(context.correlation_id, 128), span_id=_safe_text(span_id, 128),
            parent_span_id=_safe_text(parent_span_id, 128) if parent_span_id else None,
            operation=_safe_text(operation, 128), status=status,
            started_at=datetime.now(timezone.utc), duration_ms=min(float(duration_ms), 86_400_000.0),
            labels=bounded_labels, attributes=safe_attributes if isinstance(safe_attributes, Mapping) else {},
        )
        if record.export_size() > MAX_RECORD_BYTES:
            compact = TraceRecord(record.trace_id, record.span_id, record.operation, record.status,
                                   record.started_at, record.duration_ms, record.parent_span_id,
                                   record.labels, {"attributes_digest": _digest(record.attributes)})
            record = compact
        self._exporter.export(record)
        return record


class HealthState(StrEnum):
    LIVE = "live"
    READY = "ready"
    DEPENDENCY_UNHEALTHY = "dependency_unhealthy"
    DEGRADED = "degraded"
    DRAINING = "draining"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class HealthSnapshot:
    states: frozenset[HealthState]
    dependencies: Mapping[str, str] = field(default_factory=dict)
    detail: str = ""

    @property
    def healthy(self) -> bool:
        return HealthState.LIVE in self.states and HealthState.READY in self.states

    def as_dict(self) -> dict[str, object]:
        return {"states": sorted(state.value for state in self.states),
                "dependencies": dict(list(self.dependencies.items())[:MAX_LABELS]),
                "detail": _safe_text(self.detail, MAX_ATTRIBUTE_VALUE)}


def health_snapshot(*, live: bool, ready: bool, dependencies: Mapping[str, str] | None = None,
                    degraded: bool = False, draining: bool = False,
                    recovery_required: bool = False, detail: str = "") -> HealthSnapshot:
    states: set[HealthState] = set()
    if live:
        states.add(HealthState.LIVE)
    if ready:
        states.add(HealthState.READY)
    if dependencies and any(value not in {"healthy", "ready", "ok"} for value in dependencies.values()):
        states.add(HealthState.DEPENDENCY_UNHEALTHY)
    if degraded:
        states.add(HealthState.DEGRADED)
    if draining:
        states.add(HealthState.DRAINING)
    if recovery_required:
        states.add(HealthState.RECOVERY_REQUIRED)
    return HealthSnapshot(frozenset(states), dict(list((dependencies or {}).items())[:MAX_LABELS]), detail)


__all__ = ["BoundedTracer", "CardinalityLimiter", "HealthSnapshot", "HealthState",
           "TraceExporter", "TraceRecord", "health_snapshot"]
