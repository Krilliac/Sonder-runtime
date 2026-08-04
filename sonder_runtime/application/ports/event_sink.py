"""Domain event sink port (SPEC-3 section 8).

Events carry low-cardinality codes, identifiers, and state — never raw
prompts or secrets. Observability copies never determine business
success.
"""
from __future__ import annotations

from typing import Protocol


class EventSink(Protocol):
    def emit(
        self,
        event_code: str,
        *,
        summary: str,
        detail: dict | None = None,
        severity: str = "INFO",
        correlation_id: str | None = None,
        operation_id: str | None = None,
    ) -> None: ...
