"""Shared, transport-neutral event vocabulary for resumable protocols."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Mapping


class EventKind(str, Enum):
    SNAPSHOT = "snapshot"
    EVENT = "event"


@dataclass(frozen=True)
class EventEnvelope:
    stream_id: str
    sequence: int
    event_type: str
    payload: Mapping[str, Any]
    event_id: str
    kind: EventKind = EventKind.EVENT

    def __post_init__(self) -> None:
        if not self.stream_id or not self.event_type or not self.event_id:
            raise ValueError("stream_id, event_type, and event_id are required")
        if self.sequence < 0:
            raise ValueError("sequence must be non-negative")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a mapping")


@dataclass(frozen=True)
class Snapshot:
    stream_id: str
    watermark: int
    state: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not self.stream_id or self.watermark < 0:
            raise ValueError("stream_id and non-negative watermark are required")
        if not isinstance(self.state, Mapping):
            raise TypeError("state must be a mapping")


def validate_monotonic(events: tuple[EventEnvelope, ...] | list[EventEnvelope]) -> None:
    previous: int | None = None
    stream: str | None = None
    for event in events:
        if stream is None:
            stream = event.stream_id
        elif event.stream_id != stream:
            raise ValueError("events must belong to one stream")
        if previous is not None and event.sequence <= previous:
            raise ValueError("event sequences must be strictly increasing")
        previous = event.sequence
