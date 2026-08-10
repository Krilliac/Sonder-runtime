"""Logging event sink adapter (SPEC-5 WP11).

Replaces OperationsEventSink for the SPEC-5 composition root.
Events go to Python logging; the operations.db persistence adapter
(WP2) handles durable storage separately.
"""
from __future__ import annotations

import logging

logger = logging.getLogger("sonder_runtime.events")


class LoggingEventSink:
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
        level = getattr(logging, severity.upper(), logging.INFO)
        logger.log(level, "[%s] %s", event_code, summary)
