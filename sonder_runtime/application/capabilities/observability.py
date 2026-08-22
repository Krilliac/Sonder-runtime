"""Application observability capability: redact before telemetry export."""
from __future__ import annotations

from dataclasses import replace
from typing import Mapping

from ..ports.telemetry_sink import TelemetryEvent, TelemetryRedactor, TelemetrySink


class RedactingTelemetrySink:
    """Decorator that makes the redaction-before-export rule executable."""

    def __init__(self, delegate: TelemetrySink, redactor: TelemetryRedactor) -> None:
        if delegate is None or redactor is None:
            raise TypeError("delegate and redactor are required")
        self._delegate = delegate
        self._redactor = redactor

    def emit(self, event: TelemetryEvent) -> None:
        fields = {key: self._redact_value(value) for key, value in event.fields.items()}
        self._delegate.emit(replace(event, fields=fields, redaction_applied=True))

    def _redact_value(self, value: object) -> object:
        if isinstance(value, Mapping):
            return {str(key): self._redact_value(child) for key, child in value.items()}
        if isinstance(value, (list, tuple)):
            return [self._redact_value(child) for child in value]
        if isinstance(value, str):
            return self._redactor.redact(value)
        return value


__all__ = ["RedactingTelemetrySink"]
