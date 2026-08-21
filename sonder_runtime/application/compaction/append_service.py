"""Append-only persistence boundary for bounded structured compaction events."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Mapping, Sequence
from uuid import uuid4

from ..ports.session_repository import SessionEvent, SessionRepository


class CompactionAppendError(ValueError):
    """Raised when a source range or structured event is unsafe to append."""


def _freeze_strings(values: Sequence[str], *, field: str, limit: int) -> tuple[str, ...]:
    if len(values) > limit:
        raise CompactionAppendError(f"{field} exceeds the {limit}-item bound")
    result = tuple(value.strip() for value in values)
    if any(not value for value in result):
        raise CompactionAppendError(f"{field} must contain non-empty strings")
    return result


@dataclass(frozen=True)
class ImmutableSourceEventRange:
    """Exact, immutable inclusive range of source events to retain by identity."""

    session_id: str
    start_sequence: int
    end_sequence: int
    start_event_id: str
    end_event_id: str

    def __post_init__(self) -> None:
        if not self.session_id.strip() or not self.start_event_id.strip() or not self.end_event_id.strip():
            raise CompactionAppendError("source range identities are required")
        if (isinstance(self.start_sequence, bool) or isinstance(self.end_sequence, bool)
                or self.start_sequence < 1 or self.end_sequence < self.start_sequence):
            raise CompactionAppendError("source range must be non-empty and ordered")

    @property
    def count(self) -> int:
        return self.end_sequence - self.start_sequence + 1


@dataclass(frozen=True)
class StructuredCompaction:
    """Bounded, typed retention claims carried by a compaction event."""

    facts: tuple[str, ...] = ()
    decisions: tuple[str, ...] = ()
    unresolved_tasks: tuple[str, ...] = ()
    artifacts: tuple[str, ...] = ()
    tool_outcomes: tuple[str, ...] = ()
    max_items: int = 128

    def __post_init__(self) -> None:
        if isinstance(self.max_items, bool) or self.max_items < 1:
            raise CompactionAppendError("max_items must be positive")
        for field in ("facts", "decisions", "unresolved_tasks", "artifacts", "tool_outcomes"):
            object.__setattr__(self, field, _freeze_strings(getattr(self, field), field=field, limit=self.max_items))

    def as_payload(self) -> Mapping[str, object]:
        return {
            "facts": list(self.facts),
            "decisions": list(self.decisions),
            "unresolved_tasks": list(self.unresolved_tasks),
            "artifacts": list(self.artifacts),
            "tool_outcomes": list(self.tool_outcomes),
        }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class CompactionAppendService:
    """Verify an exact source range, then append one bounded compaction event."""

    def __init__(self, repository: SessionRepository, *, event_id_factory: Callable[[], str] | None = None,
                 occurred_at_factory: Callable[[], str] | None = None, max_source_events: int = 1_000) -> None:
        if max_source_events < 1:
            raise ValueError("max_source_events must be positive")
        self._repository = repository
        self._event_id_factory = event_id_factory or (lambda: f"compaction-{uuid4().hex}")
        self._occurred_at_factory = occurred_at_factory or _now
        self._max_source_events = max_source_events

    # [any thread, repository-defined thread safety]
    def append(self, source_range: ImmutableSourceEventRange,
               summary: StructuredCompaction) -> SessionEvent:
        """Append without changing or deleting any source event."""
        if source_range.count > self._max_source_events:
            raise CompactionAppendError("source range exceeds the service bound")
        events = self._repository.read_range(
            source_range.session_id,
            start_sequence=source_range.start_sequence,
            end_sequence=source_range.end_sequence,
            limit=source_range.count,
        )
        expected = source_range.count
        if len(events) != expected:
            raise CompactionAppendError("source range is truncated or unavailable")
        if any(event.session_id != source_range.session_id for event in events):
            raise CompactionAppendError("source range contains a different session")
        if tuple(event.sequence for event in events) != tuple(range(source_range.start_sequence, source_range.end_sequence + 1)):
            raise CompactionAppendError("source range is not contiguous")
        if events[0].event_id != source_range.start_event_id or events[-1].event_id != source_range.end_event_id:
            raise CompactionAppendError("source range identities do not match repository history")
        payload = {
            "source_range": {
                "session_id": source_range.session_id,
                "start_sequence": source_range.start_sequence,
                "end_sequence": source_range.end_sequence,
                "start_event_id": source_range.start_event_id,
                "end_event_id": source_range.end_event_id,
            },
            "summary": summary.as_payload(),
        }
        return self._repository.append(
            source_range.session_id,
            "compaction.completed",
            payload,
            event_id=self._event_id_factory(),
            occurred_at_utc=self._occurred_at_factory(),
        )


__all__ = ["CompactionAppendError", "CompactionAppendService", "ImmutableSourceEventRange", "StructuredCompaction"]
