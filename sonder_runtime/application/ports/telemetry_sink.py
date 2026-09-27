"""Redaction-first telemetry export port (WP3 SEAM-013)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Mapping, Protocol


@dataclass(frozen=True, slots=True)
class TelemetryEvent:
    """Bounded, export-safe event envelope."""

    event_code: str
    occurred_at: datetime
    fields: Mapping[str, object] = field(default_factory=lambda: MappingProxyType({}))
    correlation_id: str | None = None
    operation_id: str | None = None
    redaction_applied: bool = False
    # Optional envelope identities (Observatory sonder.observatory.event/1).
    # They are additive: an exporter that predates them ignores them, and
    # ``operation_id`` keeps serving as the run identity when ``run_id`` is
    # absent.
    session_id: str | None = None
    run_id: str | None = None
    agent_id: str | None = None
    task_id: str | None = None
    level: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event_code, str) or not self.event_code.strip():
            raise ValueError("event_code must be non-empty")
        if not isinstance(self.occurred_at, datetime):
            raise TypeError("occurred_at must be a datetime")
        if not isinstance(self.fields, Mapping):
            raise TypeError("fields must be a mapping")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        if not isinstance(self.redaction_applied, bool):
            raise TypeError("redaction_applied must be bool")
        for name in ("session_id", "run_id", "agent_id", "task_id", "level"):
            value = getattr(self, name)
            if value is not None and not isinstance(value, str):
                raise TypeError(f"{name} must be a string when supplied")


class TelemetrySink(Protocol):
    """Export only sanitized telemetry; export failures are adapter policy."""

    # [any thread, async safe] The event must already have crossed redaction.
    def emit(self, event: TelemetryEvent) -> None: ...


class TelemetryRedactor(Protocol):
    """Minimal redaction dependency used by the application capability."""

    def redact(self, text: str) -> str: ...


__all__ = ["TelemetryEvent", "TelemetryRedactor", "TelemetrySink"]
