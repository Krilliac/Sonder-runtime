"""In-memory reference implementation of snapshot-plus-event resumable streams."""
from __future__ import annotations

from dataclasses import dataclass
from threading import RLock

from sonder_runtime.domain.protocol.events import EventEnvelope, EventKind, Snapshot


class StreamBackpressure(Exception):
    """Raised when a producer would exceed the retained-event bound."""


class StreamGap(Exception):
    """Raised when a watermark predates retained history and no snapshot covers it."""


@dataclass(frozen=True)
class ResumeBatch:
    snapshot: Snapshot | None
    events: tuple[EventEnvelope, ...]
    next_watermark: int
    has_more: bool = False


class ResumableStream:
    """A bounded stream with duplicate suppression and replay-safe resume."""

    def __init__(self, stream_id: str, *, capacity: int = 256) -> None:
        if not stream_id or capacity < 1:
            raise ValueError("stream_id and positive capacity are required")
        self.stream_id = stream_id
        self.capacity = capacity
        self._snapshot: Snapshot | None = None
        self._events: list[EventEnvelope] = []
        self._event_ids: dict[str, EventEnvelope] = {}
        self._next_sequence = 1
        self._lock = RLock()

    @property
    def watermark(self) -> int:
        with self._lock:
            return self._next_sequence - 1

    def publish_snapshot(self, state: dict, *, watermark: int | None = None) -> Snapshot:
        with self._lock:
            point = self.watermark if watermark is None else watermark
            if point < 0 or point > self.watermark:
                raise ValueError("snapshot watermark must be within stream history")
            self._snapshot = Snapshot(self.stream_id, point, dict(state))
            self._events = [event for event in self._events if event.sequence > point]
            return self._snapshot

    def publish(self, event_type: str, payload: dict, *, event_id: str, sequence: int | None = None) -> EventEnvelope:
        with self._lock:
            if event_id in self._event_ids:
                return self._event_ids[event_id]
            point = self._next_sequence if sequence is None else sequence
            if point != self._next_sequence:
                raise ValueError(f"expected sequence {self._next_sequence}, got {point}")
            if len(self._events) >= self.capacity:
                raise StreamBackpressure("retained event capacity reached; publish a snapshot first")
            event = EventEnvelope(self.stream_id, point, event_type, dict(payload), event_id, EventKind.EVENT)
            self._events.append(event)
            self._event_ids[event_id] = event
            self._next_sequence += 1
            return event

    def resume(self, watermark: int, *, limit: int | None = None) -> ResumeBatch:
        with self._lock:
            if watermark < 0 or watermark > self.watermark:
                raise ValueError("watermark is outside stream range")
            first = self._events[0].sequence if self._events else self.watermark + 1
            snapshot = self._snapshot if watermark < (self._snapshot.watermark if self._snapshot else -1) else None
            effective = snapshot.watermark if snapshot is not None else watermark
            if not snapshot and self._events and watermark < first - 1:
                raise StreamGap("watermark predates retained event history")
            selected = tuple(event for event in self._events if event.sequence > effective)
            has_more = limit is not None and limit >= 0 and len(selected) > limit
            if limit is not None:
                selected = selected[:limit]
            next_watermark = selected[-1].sequence if selected else effective
            return ResumeBatch(snapshot, selected, next_watermark, has_more)

    def retained_events(self) -> tuple[EventEnvelope, ...]:
        with self._lock:
            return tuple(self._events)
