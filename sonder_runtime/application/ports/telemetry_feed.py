"""Live telemetry feed port (Observatory live producer protocol v1).

A feed is the read side of a bounded producer ring: subscribers resume after
an event id (or start live), receive already-serialized, already-redacted
envelope lines, and learn how many events they lost when they fell behind.
The port carries no transport: HTTP framing lives in the interface layer.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Mapping, Protocol

from ...domain.common.errors import CapacityExceeded


def rfc3339_millis(moment: datetime) -> str:
    """The protocol's wall-clock format: RFC 3339 UTC, milliseconds, ``Z``.

    Used for envelope ``wall_time`` and document timestamps such as the
    ecosystem ``generated_at``; a naive datetime is taken as UTC.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    utc = moment.astimezone(timezone.utc)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + "%03dZ" % (utc.microsecond // 1000)


class SubscriberLimitReached(CapacityExceeded):
    """The producer already serves its maximum number of live subscribers."""


@dataclass(frozen=True, slots=True)
class FeedEvent:
    """One sequenced envelope, serialized as a single JSON line."""

    event_id: str
    sequence: int
    line: str


@dataclass(frozen=True, slots=True)
class FeedBatch:
    """Events delivered to one subscriber by one read.

    ``lost`` counts events this subscriber missed because the ring overwrote
    them before it read them; it is per subscriber, never a producer drop.
    ``closed`` means the feed closed this subscription (drain or shutdown).
    """

    events: tuple[FeedEvent, ...] = ()
    lost: int = 0
    closed: bool = False


@dataclass(frozen=True, slots=True)
class ResumeGap:
    """A requested resume point that is older than the retained window."""

    first_missing: int
    last_missing: int


class TelemetrySubscription(Protocol):
    """One subscriber cursor over the producer ring."""

    @property
    def resume_gap(self) -> ResumeGap | None: ...

    def next_batch(self, timeout: float, *, limit: int = 256) -> FeedBatch: ...

    def close(self) -> None: ...


class TelemetryFeed(Protocol):
    """Read side of a live producer; every method is non-blocking except reads."""

    @property
    def instance_id(self) -> str: ...

    def subscribe(
        self, *, last_event_id: str | None = None, since_now: bool = False,
    ) -> TelemetrySubscription: ...

    def discovery(self, *, auth_required: bool) -> Mapping[str, object]: ...

    def stats(self) -> Mapping[str, int]: ...

    def close_subscribers(self) -> None: ...


__all__ = [
    "FeedBatch",
    "FeedEvent",
    "ResumeGap",
    "SubscriberLimitReached",
    "TelemetryFeed",
    "TelemetrySubscription",
    "rfc3339_millis",
]
