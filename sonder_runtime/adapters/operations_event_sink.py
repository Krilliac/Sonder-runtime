"""Durable operations event-sink adapter.

This adapter is the application-facing boundary over the packaged
``OperationsStore``.  Persistence failures are deliberately non-fatal to the
business operation: callers use the event sink for observability, while the
operations store remains the durable audit authority when it is available.
"""
from __future__ import annotations


class OperationsEventSink:
    """EventSink over the SPEC-2 operations store."""

    def __init__(self) -> None:
        self._store = None

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
        try:
            if self._store is None:
                from sonder_runtime.adapters.persistence.operations_store import (
                    OperationsStore,
                )

                self._store = OperationsStore()
            self._store.record_event(
                component="application",
                event_code=event_code,
                severity=severity,
                summary=summary,
                detail=detail,
                correlation_id=correlation_id,
                operation_id=operation_id,
            )
        except Exception:
            return
