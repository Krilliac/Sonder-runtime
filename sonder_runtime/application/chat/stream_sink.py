"""Live token forwarding for one streamed HTTP chat turn.

A streamed turn used to send the whole finished answer as one SSE chunk, so
the time to the first visible token equalled the whole generation.  When the
turn's rung is bridged to a provider that can stream (Sonder Inference), the
HTTP adapter arms a :class:`LiveTurnStream` for the turn and the provider
forwards content deltas to it as they are generated.

The pipeline around the model call can still change the answer after
generation (a code-gate repair, an escalation to another rung, the web-denial
guard).  Three rules keep what the client saw honest:

* **One generation streams.**  The first bridged generation of the turn
  claims the stream; every later model step in the turn (a repair, another
  rung) runs exactly as before, without streaming.
* **Unverified code is held back.**  With ``hold_marker`` set (the adapter
  sets it to a code fence while the chat code gate is enabled), forwarding
  stops at the first fence; the rest reaches the client only after the turn,
  so code the gate would repair is never shown as if it stood.
* **The final answer wins.**  :meth:`LiveTurnStream.reconcile` compares the
  turn's final text with what was forwarded: a continuation is sent as the
  remaining delta; anything else is announced as a revision followed by the
  final answer, never silently dropped.

Pure application code: no I/O, no environment.  The adapter supplies the
writer; the provider adapter reads :func:`call_stream`.
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Callable, Iterator

# Shown before the final answer when it no longer continues what was
# streamed.  Plain text: every client renders it, none needs to parse it.
REVISION_NOTICE = "[Sonder revised this answer after checking it; the final version follows.]"

_TURN: ContextVar["LiveTurnStream | None"] = ContextVar("sonder_live_turn_stream", default=None)
_CALL: ContextVar["LiveTurnStream | None"] = ContextVar("sonder_live_call_stream", default=None)


class LiveTurnStream:
    """Forward one generation's content deltas to a streaming client.

    ``write(text)`` sends one delta and returns ``False`` once the client is
    gone; ``client_gone()`` reports a departure noticed elsewhere (the
    keep-alive writer).  Not thread-safe by itself: the adapter's writer
    serialises frames with its own lock.
    """

    def __init__(
        self,
        write: Callable[[str], bool],
        *,
        client_gone: Callable[[], bool] | None = None,
        hold_marker: str | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._write = write
        self._client_gone = client_gone
        self._hold_marker = hold_marker or None
        self._clock = clock
        self._claimed = False
        self._held = False
        self._gone = False
        self.started_at = clock()
        self.first_delta_at: float | None = None
        self.forwarded = ""
        self.generated = ""

    @property
    def claimed(self) -> bool:
        return self._claimed

    @property
    def held(self) -> bool:
        """Whether forwarding stopped at the hold marker."""
        return self._held

    @property
    def cancelled(self) -> bool:
        """Whether the client has gone (generation should stop)."""
        if self._gone:
            return True
        if self._client_gone is not None and self._client_gone():
            self._gone = True
        return self._gone

    def claim(self) -> bool:
        """Claim the stream for one generation; ``False`` if already claimed."""
        if self._claimed:
            return False
        self._claimed = True
        return True

    def _safe_end(self, text: str) -> int:
        """End of the forwardable prefix of ``text`` under the hold rule."""
        marker = self._hold_marker
        if marker is None:
            return len(text)
        found = text.find(marker, len(self.forwarded))
        if found >= 0:
            self._held = True
            return found
        # Keep back a tail that could be the start of a split marker.
        for keep in range(min(len(marker) - 1, len(text)), 0, -1):
            if marker.startswith(text[-keep:]):
                return len(text) - keep
        return len(text)

    def emit(self, text: str) -> bool:
        """Forward one generated delta; ``False`` when the client is gone."""
        if not isinstance(text, str) or not text:
            return not self.cancelled
        self.generated += text
        if self.cancelled:
            return False
        if self._held:
            return True
        end = max(len(self.forwarded), self._safe_end(self.generated))
        piece = self.generated[len(self.forwarded):end]
        if not piece:
            return True
        if not self._write(piece):
            self._gone = True
            return False
        if self.first_delta_at is None:
            self.first_delta_at = self._clock()
        self.forwarded += piece
        return True

    def reconcile(self, final: str) -> tuple[str, bool]:
        """``(text still to send, revised)`` for the turn's final answer."""
        final = str(final or "")
        sent = self.forwarded
        if not sent:
            return final, False
        if final.startswith(sent):
            return final[len(sent):], False
        if sent.rstrip() == final.rstrip() or (
            sent.startswith(final) and not sent[len(final):].strip()
        ):
            # Only trailing whitespace differs; the client already has it all.
            return "", False
        return "\n\n%s\n\n%s" % (REVISION_NOTICE, final), True

    def ttft_ms(self) -> float | None:
        """Milliseconds from arming to the first forwarded delta, if any."""
        if self.first_delta_at is None:
            return None
        return max(0.0, (self.first_delta_at - self.started_at) * 1000.0)


@contextmanager
def armed(stream: LiveTurnStream | None) -> Iterator[LiveTurnStream | None]:
    """Arm ``stream`` for the enclosed turn (``None`` arms nothing)."""
    token = _TURN.set(stream)
    try:
        yield stream
    finally:
        _TURN.reset(token)


def turn_stream() -> LiveTurnStream | None:
    """The stream armed for the current turn, claimed or not."""
    return _TURN.get()


@contextmanager
def claimed_for_call() -> Iterator[LiveTurnStream | None]:
    """Claim the turn's stream for one provider call when it is still free.

    Yields the stream the provider must forward to, or ``None`` (nothing
    armed, or an earlier generation of this turn already streamed).
    """
    stream = _TURN.get()
    selected = stream if stream is not None and stream.claim() else None
    token = _CALL.set(selected)
    try:
        yield selected
    finally:
        _CALL.reset(token)


def call_stream() -> LiveTurnStream | None:
    """The stream the provider call in progress should forward to, if any."""
    return _CALL.get()


__all__ = [
    "LiveTurnStream",
    "REVISION_NOTICE",
    "armed",
    "call_stream",
    "claimed_for_call",
    "turn_stream",
]
