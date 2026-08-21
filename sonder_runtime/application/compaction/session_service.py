"""Durable session compaction application service."""
from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any
from uuid import uuid4

from .legacy import CompactionApplicationService
from ..ports.compaction import CompactionRequest, SessionHistoryEvent, SourceRange
from ..ports.session_repository import SessionEvent, SessionRepository


class SessionCompactionError(ValueError):
    """Raised when durable session history cannot satisfy a compaction request."""


def _json_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class SessionCompactionService:
    """Read, validate, compact, and append one durable session event."""

    def __init__(
        self,
        repository: SessionRepository,
        *,
        event_id_factory: Callable[[], str] | None = None,
        max_events: int = 1_000,
    ) -> None:
        if isinstance(max_events, bool) or max_events < 1:
            raise ValueError("max_events must be positive")
        self._repository = repository
        self._event_id_factory = event_id_factory or (lambda: f"compaction-{uuid4().hex}")
        self._max_events = max_events

    def compact(
        self,
        session_id: str,
        *,
        start_sequence: int,
        end_sequence: int,
        max_summary_tokens: int | None = None,
    ) -> SessionEvent:
        if not session_id.strip():
            raise SessionCompactionError("session_id is required")
        if (
            isinstance(start_sequence, bool)
            or isinstance(end_sequence, bool)
            or start_sequence < 1
            or end_sequence < start_sequence
        ):
            raise SessionCompactionError("source range must be non-empty and ordered")
        count = end_sequence - start_sequence + 1
        if count > self._max_events:
            raise SessionCompactionError("source range exceeds the service bound")
        events = self._repository.read_range(
            session_id,
            start_sequence=start_sequence,
            end_sequence=end_sequence,
            limit=count,
        )
        if len(events) != count or tuple(event.sequence for event in events) != tuple(
            range(start_sequence, end_sequence + 1)
        ):
            raise SessionCompactionError("source range is truncated or non-contiguous")
        source = SourceRange(
            session_id, start_sequence, end_sequence,
            events[0].event_id, events[-1].event_id,
        )
        request = CompactionRequest(
            session_id,
            tuple(self._history_event(event) for event in events),
            source,
            max_summary_tokens=max_summary_tokens,
        )
        result = CompactionApplicationService(
            event_id_factory=self._event_id_factory,
        ).compact(request)
        if not result.validation.valid:
            raise SessionCompactionError(result.validation.detail)
        summary = result.summary
        payload = {
            "source_range": {
                "session_id": source.session_id,
                "start_sequence": source.start_sequence,
                "end_sequence": source.end_sequence,
                "start_event_id": source.start_event_id,
                "end_event_id": source.end_event_id,
            },
            "summary": {
                "facts": list(summary.facts),
                "decisions": list(summary.decisions),
                "unresolved_tasks": list(summary.unresolved_tasks),
                "artifacts": list(summary.artifacts),
                "tool_outcomes": list(summary.tool_outcomes),
                "confidence": summary.confidence,
                "modalities": [
                    {
                        "event_id": item.event_id,
                        "event_type": item.event_type,
                        "modality": item.modality,
                        "payload": _json_value(item.payload),
                    }
                    for item in summary.modalities
                ],
            },
        }
        return self._repository.append(
            session_id,
            "compaction.completed",
            payload,
            event_id=result.appended_event.event_id,
        )

    @staticmethod
    def _history_event(event: SessionEvent) -> SessionHistoryEvent:
        payload = dict(event.payload)
        modality = payload.get("modality", "text")
        if not isinstance(modality, str) or not modality.strip():
            modality = "text"
        return SessionHistoryEvent(
            event.event_id,
            event.session_id,
            event.sequence,
            event.event_type,
            payload,
            modality,
        )


__all__ = ["SessionCompactionError", "SessionCompactionService"]
