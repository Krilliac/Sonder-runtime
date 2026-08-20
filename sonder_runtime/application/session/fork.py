"""Pure planning for explicit event-boundary session forks (WP2 SESSION-006)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, overload

from ...domain.common.errors import IntegrityFailure, InvalidInput
from ...domain.common.events import DomainEvent
from ...domain.common.ids import SessionId


@dataclass(frozen=True, slots=True)
class ForkBoundary:
    sequence: int
    event_id: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.sequence, bool) or not isinstance(self.sequence, int) or self.sequence < 1:
            raise InvalidInput("fork boundary sequence must be a positive integer")
        if self.event_id is not None and (not isinstance(self.event_id, str) or not self.event_id.strip()):
            raise InvalidInput("fork boundary event_id must be non-empty text")


@dataclass(frozen=True, slots=True)
class SessionLineage:
    parent_session_id: SessionId | str
    boundary_sequence: int
    boundary_event_id: str
    child_session_id: SessionId | str

    @property
    def session_id(self) -> str:
        return self.child_session_id.serialize() if isinstance(self.child_session_id, SessionId) else self.child_session_id

    @property
    def fork_sequence(self) -> int:
        return self.boundary_sequence


@dataclass(frozen=True, slots=True)
class SessionFork:
    lineage: SessionLineage
    inherited_events: tuple[DomainEvent, ...]

    @property
    def child_session_id(self) -> SessionId | str:
        return self.lineage.child_session_id

    @property
    def next_sequence(self) -> int:
        return self.lineage.boundary_sequence + 1

    def __iter__(self):
        yield self.lineage
        yield self.inherited_events


def _ordered(events: Iterable[DomainEvent]) -> tuple[DomainEvent, ...]:
    values = tuple(events)
    if not values or any(not isinstance(event, DomainEvent) for event in values):
        raise InvalidInput("session fork requires non-empty DomainEvent records")
    if any(isinstance(event.sequence, bool) or not isinstance(event.sequence, int) for event in values):
        raise InvalidInput("session fork event sequences must be integers")
    ordered = tuple(sorted(values, key=lambda event: event.sequence))
    parent = ordered[0].aggregate_id
    if any(event.aggregate_type != "session" or event.aggregate_id != parent for event in ordered):
        raise IntegrityFailure("session fork contains multiple or non-session aggregates")
    sequences = tuple(event.sequence for event in ordered)
    if sequences != tuple(range(1, len(ordered) + 1)):
        raise IntegrityFailure("session fork source stream has a gap or duplicate sequence")
    return ordered


@overload
def fork_session(events: Iterable[DomainEvent], boundary: ForkBoundary | int, *, child_session_id: SessionId | None = None) -> SessionFork: ...


@overload
def fork_session(session_id: str, events: Iterable[DomainEvent], *, fork_sequence: int, child_session_id: str) -> SessionFork: ...


def fork_session(source, boundary_or_events, *, fork_sequence=None, child_session_id=None):
    """Plan a child fork; the boundary is inclusive and must exist in the stream."""
    legacy = isinstance(source, str)
    if legacy:
        parent_raw, events = source, boundary_or_events
        if fork_sequence is None or not isinstance(child_session_id, str):
            raise InvalidInput("legacy fork calls require an explicit boundary and child ID")
        boundary = ForkBoundary(fork_sequence)
        ordered = _ordered(events)
        if any(event.aggregate_id != parent_raw for event in ordered):
            raise IntegrityFailure("fork events must belong to the parent session")
        parent: SessionId | str = parent_raw
        child: SessionId | str = child_session_id
    else:
        boundary = ForkBoundary(boundary_or_events) if isinstance(boundary_or_events, int) else boundary_or_events
        if not isinstance(boundary, ForkBoundary):
            raise InvalidInput("fork boundary must be ForkBoundary or sequence")
        ordered = _ordered(source)
        try:
            parent = SessionId.from_serialized(ordered[0].aggregate_id)
        except ValueError as exc:
            raise InvalidInput("session fork requires a typed parent SessionId") from exc
        child = SessionId.new() if child_session_id is None else child_session_id
        if not isinstance(child, SessionId):
            raise InvalidInput("child_session_id must be a SessionId")
    if boundary.sequence > len(ordered):
        raise InvalidInput("fork boundary is beyond the source stream")
    event = ordered[boundary.sequence - 1]
    if boundary.event_id is not None and event.id != boundary.event_id:
        raise IntegrityFailure("fork boundary event_id does not match its sequence")
    if child == parent:
        raise InvalidInput("child session must differ from parent session")
    return SessionFork(SessionLineage(parent, boundary.sequence, event.id, child), ordered[: boundary.sequence])


__all__ = ["ForkBoundary", "SessionFork", "SessionLineage", "fork_session"]
