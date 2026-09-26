"""Runtime as an Observatory live producer (live producer protocol v1).

``ObservatoryProducer`` is both the ``TelemetrySink`` at the end of the
redacting export chain and the ``TelemetryFeed`` the HTTP stream routes read.
It turns each already-redacted ``TelemetryEvent`` into one
``sonder.observatory.event/1`` envelope line, numbers it (contiguous per
process instance, from 0), and appends it to a bounded ring.

Guarantees the protocol depends on:

* ``emit`` does no I/O, never raises, and holds the ring lock only to number
  and append one pre-serialized line, so its cost does not depend on how many
  subscribers exist or how slow they are.
* Subscribers are cursors into the ring.  A subscriber that falls behind loses
  its oldest undelivered events; the loss is reported to that subscriber only
  (``FeedBatch.lost``) and counted as ``subscriber_dropped_events``.  It is not
  a producer drop and is never published as ``telemetry.dropped``.
* ``telemetry.dropped`` reports events the producer itself could not sequence
  (an envelope that failed to serialize), with a cumulative count.
"""
from __future__ import annotations

import collections
import itertools
import json
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Mapping

from ...application.ports.telemetry_feed import (
    FeedBatch,
    FeedEvent,
    ResumeGap,
    SubscriberLimitReached,
    rfc3339_millis,
)
from ...application.ports.telemetry_sink import TelemetryEvent

EVENT_SCHEMA = "sonder.observatory.event/1"
DISCOVERY_SCHEMA = "sonder.telemetry.producer/1"
PRODUCER_NAME = "sonder-runtime"
PRODUCER_ROLE = "runtime"
DEFAULT_BUFFER = 4096
MIN_BUFFER = 256
MAX_BUFFER = 65536
DEFAULT_MAX_SUBSCRIBERS = 8
EVENTS_PATH = "/v1/observability/events"
DISCOVERY_PATH = "/.well-known/sonder-telemetry"
ECOSYSTEM_PATH = "/v1/sonder/ecosystem"
TRACE_PATH = "/v1/observability/trace"
SAMPLING_LEVEL = "metrics"
TEXT_CAPTURE = "none"
VOCABULARIES = {"sonder.runtime.events": 1}


def clamp_buffer(value: object) -> int:
    """Clamp a configured ring size to the supported 256..65536 range."""
    try:
        number = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_BUFFER
    return max(MIN_BUFFER, min(MAX_BUFFER, number))


def parse_event_id(value: object) -> tuple[str, int] | None:
    """Split ``<instance_id>-<sequence>`` at the last ``-``."""
    if not isinstance(value, str) or "-" not in value:
        return None
    instance, _, raw = value.strip().rpartition("-")
    if not instance or not raw.isdigit() or len(raw) > 19:
        return None
    return instance, int(raw)


class _Subscription:
    """One cursor into the producer ring (see TelemetrySubscription)."""

    def __init__(self, producer: "ObservatoryProducer", next_sequence: int,
                 gap: ResumeGap | None) -> None:
        self._producer = producer
        self._next = next_sequence
        self._gap = gap
        self._closed = False
        # Set when the producer closes this subscription: events sequenced
        # before that point are still delivered (session.ended and the final
        # telemetry.dropped are emitted just before a close), then the batch
        # reports ``closed``.  A subscriber that closes itself drains nothing.
        self._close_at: int | None = None

    @property
    def resume_gap(self) -> ResumeGap | None:
        return self._gap

    @property
    def closed(self) -> bool:
        return self._closed

    def _end_locked(self) -> int:
        end = self._producer._next_sequence
        if self._closed:
            end = min(end, self._close_at if self._close_at is not None else self._next)
        return end

    def next_batch(self, timeout: float, *, limit: int = 256) -> FeedBatch:
        producer = self._producer
        limit = max(1, min(int(limit), 4096))
        deadline = time.monotonic() + max(0.0, float(timeout))
        with producer._cond:
            while self._next >= self._end_locked():
                if self._closed:
                    return FeedBatch(closed=True)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return FeedBatch()
                producer._cond.wait(remaining)
            end = self._end_locked()
            oldest = producer._oldest_locked()
            lost = 0
            if self._next < oldest:
                lost = oldest - self._next
                producer._subscriber_dropped += lost
                self._next = oldest
            count = max(0, min(limit, end - self._next))
            start = self._next - oldest
            events = tuple(itertools.islice(producer._ring, start, start + count))
            self._next += len(events)
        return FeedBatch(events=events, lost=lost)

    def close(self) -> None:
        producer = self._producer
        with producer._cond:
            if not self._closed:
                self._closed = True
                producer._subscribers.discard(self)
                producer._cond.notify_all()


class ObservatoryProducer:
    """Bounded Observatory producer: TelemetrySink in, TelemetryFeed out."""

    def __init__(
        self,
        *,
        version: str,
        capacity: int = DEFAULT_BUFFER,
        max_subscribers: int = DEFAULT_MAX_SUBSCRIBERS,
        node_id: str | None = None,
        instance_hex: str | None = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        hex12 = instance_hex or secrets.token_hex(6)
        if len(hex12) != 12 or any(c not in "0123456789abcdef" for c in hex12):
            raise ValueError("instance_hex must be 12 lowercase hex characters")
        self._instance_id = "rt-" + hex12
        self._session_id = "rts-" + hex12
        self._version = str(version or "")
        self._node_id = node_id if node_id is not None else socket.gethostname()
        self._capacity = clamp_buffer(capacity)
        self._max_subscribers = max(1, min(int(max_subscribers), 64))
        self._monotonic_ns = monotonic_ns
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._producer_block = {
            "name": PRODUCER_NAME,
            "version": self._version,
            "node_id": self._node_id,
            "instance_id": self._instance_id,
            "role": PRODUCER_ROLE,
            "synthetic": False,
        }
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._ring: collections.deque[FeedEvent] = collections.deque(maxlen=self._capacity)
        self._next_sequence = 0
        self._dropped = 0
        self._subscriber_dropped = 0
        self._subscribers: set[_Subscription] = set()
        self._reporting_drop = threading.local()

    # -- identity ------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        return self._instance_id

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def node_id(self) -> str:
        return self._node_id

    @property
    def version(self) -> str:
        return self._version

    @property
    def capacity(self) -> int:
        return self._capacity

    # -- TelemetrySink ---------------------------------------------------------

    def _body(self, event: TelemetryEvent) -> str:
        run_id = event.run_id if event.run_id is not None else event.operation_id
        body = {
            "schema": EVENT_SCHEMA,
            "event_type": event.event_code,
            "wall_time": rfc3339_millis(event.occurred_at),
            "session_id": event.session_id or self._session_id,
            "run_id": run_id,
            "request_id": event.correlation_id,
            "agent_id": event.agent_id,
            "task_id": event.task_id,
            "producer": self._producer_block,
            "sampling": {"level": SAMPLING_LEVEL, "sampled": True},
            "attributes": dict(event.fields),
        }
        text = json.dumps(body, separators=(",", ":"), allow_nan=False,
                          ensure_ascii=False)
        if "\n" in text or "\r" in text:
            # json.dumps escapes control characters; this is a guard against
            # a future serializer change breaking one-envelope-per-line.
            raise ValueError("envelope serialized across lines")
        return text

    def emit(self, event: TelemetryEvent) -> None:
        try:
            body = self._body(event)
        except Exception:
            self._record_drop()
            return
        # Everything but the ids and the clock is serialized outside the lock;
        # the lock covers numbering, one clock read and one append, so the
        # emission cost is constant.  ``mono_ns`` is read under the same lock
        # that assigns ``sequence``, so ordering by (mono_ns, sequence) -- as
        # Observatory replays -- always agrees with ordering by sequence.
        rest = body[1:]
        with self._cond:
            try:
                mono_ns = int(self._monotonic_ns())
            except Exception:
                mono_ns = None
            else:
                sequence = self._next_sequence
                self._next_sequence += 1
                event_id = "%s-%d" % (self._instance_id, sequence)
                line = '{"event_id":"%s","sequence":%d,"mono_ns":%d,%s' % (
                    event_id, sequence, mono_ns, rest,
                )
                self._ring.append(FeedEvent(event_id, sequence, line))
                if self._subscribers:
                    self._cond.notify_all()
        if mono_ns is None:
            # A clock that cannot be read drops the event before it is
            # numbered (and counts it), like an unserializable envelope.
            self._record_drop()

    def _record_drop(self, *, final: bool = False) -> None:
        with self._lock:
            if not final:
                self._dropped += 1
            dropped, emitted = self._dropped, self._next_sequence
        if getattr(self._reporting_drop, "active", False):
            return
        self._reporting_drop.active = True
        try:
            self.emit(TelemetryEvent(
                event_code="telemetry.dropped",
                occurred_at=self._clock(),
                fields={
                    "dropped_events": dropped,
                    "emitted_events": emitted,
                    "queue_capacity": self._capacity,
                    "final": final,
                },
                redaction_applied=True,
            ))
        finally:
            self._reporting_drop.active = False

    def report_final(self) -> None:
        """Emit a final ``telemetry.dropped`` when this instance ever dropped."""
        with self._lock:
            dropped = self._dropped
        if dropped:
            self._record_drop(final=True)

    # -- TelemetryFeed ---------------------------------------------------------

    def _oldest_locked(self) -> int:
        return self._ring[0].sequence if self._ring else self._next_sequence

    def subscribe(
        self, *, last_event_id: str | None = None, since_now: bool = False,
    ) -> _Subscription:
        with self._cond:
            if len(self._subscribers) >= self._max_subscribers:
                raise SubscriberLimitReached(
                    "telemetry subscriber limit reached (%d)" % self._max_subscribers
                )
            oldest = self._oldest_locked()
            gap = None
            if since_now:
                start = self._next_sequence
            else:
                start = oldest
                parsed = parse_event_id(last_event_id)
                if parsed is not None and parsed[0] == self._instance_id:
                    requested = parsed[1] + 1
                    if oldest <= requested <= self._next_sequence:
                        start = requested
                    elif requested < oldest:
                        gap = ResumeGap(requested, oldest - 1)
            subscription = _Subscription(self, start, gap)
            self._subscribers.add(subscription)
            return subscription

    def stats(self) -> Mapping[str, int]:
        with self._lock:
            return {
                "subscribers": len(self._subscribers),
                "max_subscribers": self._max_subscribers,
                "emitted_events": self._next_sequence,
                "dropped_events": self._dropped,
                "subscriber_dropped_events": self._subscriber_dropped,
                "retained_events": len(self._ring),
                "buffer_capacity": self._capacity,
            }

    def discovery(self, *, auth_required: bool) -> Mapping[str, object]:
        with self._lock:
            retained = len(self._ring)
            oldest = self._ring[0].sequence if self._ring else None
            next_sequence = self._next_sequence
        return {
            "schema": DISCOVERY_SCHEMA,
            "producer": dict(self._producer_block),
            "event_schema": EVENT_SCHEMA,
            "streams": [
                {"transport": "sse", "url": EVENTS_PATH},
                {"transport": "ndjson", "url": EVENTS_PATH + "?format=ndjson"},
            ],
            "resume": {
                "header": "Last-Event-ID",
                "query": "last_event_id",
                "retained_events": retained,
                "oldest_sequence": oldest,
                "next_sequence": next_sequence,
            },
            "auth": {"required": bool(auth_required), "schemes": ["bearer"]},
            "clock": {"mono_ns": "host-monotonic"},
            "sampling_level": SAMPLING_LEVEL,
            "text_capture": TEXT_CAPTURE,
            "links": {"ecosystem": ECOSYSTEM_PATH, "trace": TRACE_PATH},
            "vocabularies": dict(VOCABULARIES),
            "stats": dict(self.stats()),
        }

    def close_subscribers(self) -> None:
        """Close every open subscription (drain/shutdown); new ones still work.

        Each closed subscription first receives the events sequenced before
        the close, so an event emitted just before it (``session.ended``)
        reaches every connected subscriber.
        """
        with self._cond:
            for subscription in list(self._subscribers):
                # Everything sequenced so far (session.ended included) is
                # still delivered; the stream ends after it.
                subscription._close_at = self._next_sequence
                subscription._closed = True
            self._subscribers.clear()
            self._cond.notify_all()


__all__ = [
    "DEFAULT_BUFFER",
    "DEFAULT_MAX_SUBSCRIBERS",
    "DISCOVERY_PATH",
    "DISCOVERY_SCHEMA",
    "ECOSYSTEM_PATH",
    "EVENTS_PATH",
    "EVENT_SCHEMA",
    "MAX_BUFFER",
    "MIN_BUFFER",
    "ObservatoryProducer",
    "PRODUCER_NAME",
    "clamp_buffer",
    "parse_event_id",
    "rfc3339_millis",
]
