"""Transport-neutral framing for the Observatory live telemetry routes.

``serve.py`` owns sockets, authorization and CORS; this module owns the
protocol translation it would otherwise inline: format negotiation, resume
parameters, and the SSE / NDJSON frames of live producer protocol v1
(Observatory ``docs/TELEMETRY_PROTOCOL.md``).  It imports no socket or HTTP
module, so every frame can be tested without a server.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, Iterator, Mapping, Sequence

from ....application.ports.telemetry_feed import FeedEvent, TelemetrySubscription

EVENTS_ROUTE = "/v1/observability/events"
DISCOVERY_ROUTE = "/.well-known/sonder-telemetry"
ECOSYSTEM_ROUTE = "/v1/sonder/ecosystem"
TELEMETRY_ROUTES = frozenset({EVENTS_ROUTE, DISCOVERY_ROUTE, ECOSYSTEM_ROUTE})

SSE_CONTENT_TYPE = "text/event-stream; charset=utf-8"
NDJSON_CONTENT_TYPE = "application/x-ndjson"
SSE_RETRY_MS = 2000
HEARTBEAT_SECONDS = 15.0
POLL_SECONDS = 1.0


class StreamRequestError(ValueError):
    """A malformed stream request (answered with 400)."""


@dataclass(frozen=True, slots=True)
class StreamRequest:
    format: str
    last_event_id: str | None
    since_now: bool

    @property
    def content_type(self) -> str:
        return SSE_CONTENT_TYPE if self.format == "sse" else NDJSON_CONTENT_TYPE


def _first(query: Mapping[str, Sequence[str]], name: str) -> str | None:
    values = query.get(name) or ()
    return values[0] if values else None


def negotiate_stream(
    query: Mapping[str, Sequence[str]],
    *,
    accept: str = "",
    last_event_id_header: str | None = None,
) -> StreamRequest:
    """Resolve format and resume point for one stream request.

    Format: ``?format=ndjson|sse`` first, then ``Accept`` (NDJSON only when the
    client asks for it and not for SSE), else SSE.  Resume: the
    ``Last-Event-ID`` header wins over ``?last_event_id=``; ``?since=now``
    starts live.
    """
    requested = _first(query, "format")
    if requested is not None:
        fmt = requested.strip().lower()
        if fmt not in ("sse", "ndjson"):
            raise StreamRequestError("format must be sse or ndjson")
    else:
        lowered = (accept or "").lower()
        fmt = (
            "ndjson"
            if "application/x-ndjson" in lowered and "text/event-stream" not in lowered
            else "sse"
        )
    since = _first(query, "since")
    if since is not None and since.strip().lower() != "now":
        raise StreamRequestError("since accepts only 'now'")
    header = (last_event_id_header or "").strip() or None
    last_event_id = header or ((_first(query, "last_event_id") or "").strip() or None)
    if last_event_id is not None and len(last_event_id) > 256:
        raise StreamRequestError("last event id is too long")
    return StreamRequest(fmt, last_event_id, since is not None)


def sse_comment(text: str) -> bytes:
    return (": %s\n\n" % text.replace("\n", " ")).encode("utf-8")


def sse_event(event: FeedEvent) -> bytes:
    return ("id: %s\ndata: %s\n\n" % (event.event_id, event.line)).encode("utf-8")


def ndjson_event(event: FeedEvent) -> bytes:
    return (event.line + "\n").encode("utf-8")


def stream_frames(
    subscription: TelemetrySubscription,
    fmt: str,
    *,
    should_stop: Callable[[], bool],
    heartbeat_seconds: float = HEARTBEAT_SECONDS,
    poll_seconds: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Iterator[bytes]:
    """Yield wire frames until the feed closes the subscription or ``should_stop``.

    SSE opens with ``retry: 2000``, announces a stale resume point as
    ``: resume-gap <from>-<to>`` and a per-subscriber loss as ``: dropped <n>``,
    and sends ``: keepalive`` when idle.  NDJSON sends one envelope per line
    and a blank line as heartbeat; its gaps are visible as sequence jumps.
    """
    if fmt not in ("sse", "ndjson"):
        raise ValueError("unknown stream format %r" % fmt)
    sse = fmt == "sse"
    if sse:
        yield ("retry: %d\n\n" % SSE_RETRY_MS).encode("ascii")
        gap = subscription.resume_gap
        if gap is not None:
            yield sse_comment("resume-gap %d-%d" % (gap.first_missing, gap.last_missing))
    last_write = clock()
    while not should_stop():
        batch = subscription.next_batch(poll_seconds)
        if batch.closed:
            return
        chunks: list[bytes] = []
        if batch.lost and sse:
            chunks.append(sse_comment("dropped %d" % batch.lost))
        for event in batch.events:
            chunks.append(sse_event(event) if sse else ndjson_event(event))
        if chunks:
            yield b"".join(chunks)
            last_write = clock()
        elif clock() - last_write >= heartbeat_seconds:
            yield sse_comment("keepalive") if sse else b"\n"
            last_write = clock()


__all__ = [
    "DISCOVERY_ROUTE",
    "ECOSYSTEM_ROUTE",
    "EVENTS_ROUTE",
    "HEARTBEAT_SECONDS",
    "NDJSON_CONTENT_TYPE",
    "SSE_CONTENT_TYPE",
    "StreamRequest",
    "StreamRequestError",
    "TELEMETRY_ROUTES",
    "ndjson_event",
    "negotiate_stream",
    "sse_comment",
    "sse_event",
    "stream_frames",
]
