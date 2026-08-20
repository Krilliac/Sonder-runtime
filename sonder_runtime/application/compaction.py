"""Application compaction engine over immutable session-history snapshots.

This module implements the ``application.ports.compaction`` contract without
loading or changing session state.  It is deliberately deterministic except
for the injected append-event identity factory: callers may persist the
returned event, while the original history remains available for re-compaction.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from uuid import uuid4

from .ports.compaction import (
    CompactionEngine,
    CompactionEvent,
    CompactionRequest,
    CompactionResult,
    CompactionSummary,
    CompactionValidation,
    CompactionValidationError,
    SessionHistoryEvent,
    validate_compaction_result,
)


_STRUCTURED_FIELDS = (
    "facts", "decisions", "unresolved_tasks", "artifacts", "tool_outcomes",
)
def _strings(value: object) -> tuple[str, ...]:
    """Read a scalar or sequence of non-empty strings, ignoring other values."""
    if isinstance(value, str):
        return (value.strip(),) if value.strip() else ()
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray, Mapping)):
        return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())
    return ()


def _source_events(request: CompactionRequest) -> tuple[SessionHistoryEvent, ...]:
    return tuple(
        event for event in request.history
        if request.source_range.start_sequence <= event.sequence <= request.source_range.end_sequence
    )


def _summary_from(events: tuple[SessionHistoryEvent, ...], max_tokens: int | None) -> CompactionSummary:
    values: dict[str, list[str]] = {field: [] for field in _STRUCTURED_FIELDS}
    modalities: list[SessionHistoryEvent] = []
    confidence_values: list[float] = []

    for event in events:
        payload = event.payload
        for field in _STRUCTURED_FIELDS:
            values[field].extend(_strings(payload.get(field)))
        confidence = payload.get("confidence")
        if isinstance(confidence, (int, float)) and not isinstance(confidence, bool) and 0 <= confidence <= 1:
            confidence_values.append(float(confidence))
        if event.modality != "text" or event.event_type not in {"message.received", "message.sent"}:
            modalities.append(event)

    # Preserve first-seen order while avoiding duplicate retention claims.
    unique = {field: tuple(dict.fromkeys(items)) for field, items in values.items()}
    confidence = min(confidence_values) if confidence_values else None
    summary = CompactionSummary(
        facts=unique["facts"], decisions=unique["decisions"],
        unresolved_tasks=unique["unresolved_tasks"], artifacts=unique["artifacts"],
        tool_outcomes=unique["tool_outcomes"], modalities=tuple(modalities),
        confidence=confidence,
    )
    if max_tokens is not None:
        # This is a conservative, provider-independent budget check.  The port
        # intentionally carries the limit but does not prescribe tokenization.
        estimate = sum(len(item.split()) for field in _STRUCTURED_FIELDS for item in getattr(summary, field))
        estimate += sum(len(" ".join(_strings(item.payload.get("text"))).split()) for item in modalities)
        if estimate > max_tokens:
            raise CompactionValidationError("structured summary exceeds max_summary_tokens")
    return summary


def _facts(events: tuple[SessionHistoryEvent, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(item for event in events for item in _strings(event.payload.get("facts"))))


class CompactionApplicationService:
    """Deterministic append-only implementation of :class:`CompactionEngine`."""

    def __init__(self, event_id_factory: Callable[[], str] | None = None) -> None:
        self._event_id_factory = event_id_factory or (lambda: f"compaction-{uuid4().hex}")

    def compact(self, request: CompactionRequest) -> CompactionResult:
        """Build a summary and candidate event without mutating ``request``."""
        events = _source_events(request)
        summary = _summary_from(events, request.max_summary_tokens)
        event = CompactionEvent(
            self._event_id_factory(), request.session_id, request.source_range, summary,
        )
        result = CompactionResult(
            request.session_id, request.source_range, summary, event,
            self._evaluate(request, summary, events),
        )
        validate_compaction_result(request, result)
        return result

    def validate(self, request: CompactionRequest, result: CompactionResult) -> CompactionValidation:
        """Evaluate factual retention against the exact original source range."""
        validate_compaction_result(request, result)
        return self._evaluate(request, result.summary, _source_events(request))

    @staticmethod
    def _evaluate(
        request: CompactionRequest,
        summary: CompactionSummary,
        events: tuple[SessionHistoryEvent, ...],
    ) -> CompactionValidation:
        expected = _facts(events)
        retained = tuple(fact for fact in expected if fact in summary.facts)
        missing = tuple(fact for fact in expected if fact not in summary.facts)
        valid = not missing
        detail = "all source facts retained" if valid else "source facts missing from summary"
        return CompactionValidation(valid, retained, missing, detail)


DeterministicCompactionEngine = CompactionApplicationService

__all__ = ["CompactionApplicationService", "DeterministicCompactionEngine", "CompactionEngine"]
