"""Bridge selected EventSink events into the redacting telemetry export.

The EventSink is the durable audit path (operations.db) and carries free text
in ``summary`` and arbitrary structure in ``detail``.  Only an explicit
allowlist of content-free application event codes crosses into live
telemetry; everything else -- including authentication failures, which can
carry client addresses, and permission receipts, which name paths -- stays
local.  For bridged events ``summary`` is never exported and ``detail`` is
sanitized with the LocalObservabilitySink field rules before it reaches the
``RedactingTelemetrySink``.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from ...application.observability.runtime_telemetry import sanitize_correlation_id
from ...application.ports.telemetry_sink import TelemetryEvent, TelemetrySink
from ..local_observability import SEVERITIES, sanitize_event_detail

EXPORTED_EVENT_CODES = frozenset({
    "model.escalation.decided",
    "model.escalation.outcome",
    "agent.delegation.accepted",
})


class EventSinkTelemetryBridge:
    """EventSink-shaped delegate that forwards allowlisted codes to telemetry."""

    def __init__(
        self,
        telemetry: TelemetrySink,
        redactor,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if telemetry is None or redactor is None:
            raise TypeError("telemetry sink and redactor are required")
        self._telemetry = telemetry
        self._redactor = redactor
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self.rejected_events = 0

    def emit(
        self,
        event_code: str,
        *,
        summary: str,
        detail: dict | None = None,
        severity: str = "INFO",
        correlation_id: str | None = None,
        operation_id: str | None = None,
    ) -> None:
        del summary  # free text: never exported
        if event_code not in EXPORTED_EVENT_CODES:
            return
        try:
            fields = sanitize_event_detail(detail, self._redactor)
            level = severity.upper() if isinstance(severity, str) else "INFO"
            fields["severity"] = level if level in SEVERITIES else "INFO"
            self._telemetry.emit(TelemetryEvent(
                event_code=event_code,
                occurred_at=self._clock(),
                fields=fields,
                correlation_id=sanitize_correlation_id(correlation_id),
                run_id=sanitize_correlation_id(operation_id),
                level=fields["severity"],
            ))
        except Exception:
            self.rejected_events += 1


class TeeEventSink:
    """Deliver each event to the authoritative sink, then to optional siblings.

    The primary is called first with the untouched event and its exceptions
    propagate exactly as before; a sibling can neither suppress nor fail the
    durable write.
    """

    def __init__(self, primary, *siblings) -> None:
        if primary is None:
            raise TypeError("a primary EventSink is required")
        self._primary = primary
        self._siblings = tuple(sibling for sibling in siblings if sibling is not None)

    def emit(self, event_code: str, **kwargs) -> None:
        try:
            self._primary.emit(event_code, **kwargs)
        finally:
            for sibling in self._siblings:
                try:
                    sibling.emit(event_code, **kwargs)
                except Exception:
                    pass


__all__ = ["EXPORTED_EVENT_CODES", "EventSinkTelemetryBridge", "TeeEventSink"]
