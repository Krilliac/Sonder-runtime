"""SSE keep-alive while a streamed chat turn is still generating.

The streamed chat path used to write nothing -- not even the response headers
-- until the whole model turn had finished, so on a CPU host a client with an
idle timeout shorter than one generation gave up before the first byte.

The generation pipeline behind the served route (retrieval, critic and retry
passes, escalation) produces its answer only when the turn completes, so the
adapter cannot yet forward real token deltas.  What it can do is commit to the
event stream as soon as the request is admitted, and send SSE comment lines
(``: keep-alive``) on a fixed interval until the answer is ready.  Comment
lines are ignored by every conforming SSE/OpenAI client; they exist only to
keep the connection and any proxy in between from idling out.
"""
from __future__ import annotations

import threading
from typing import Callable

KEEPALIVE_FRAME = b": keep-alive\n\n"
# A status frame with no text: tells the client the wait it was shown has ended.
STATUS_CLEAR_FRAME = b": sonder-status\n\n"
STATUS_FRAME_PREFIX = ": sonder-status "
_MAX_STATUS_CHARS = 200


def status_frame(text: str) -> bytes:
    """One SSE comment frame carrying ``text`` (a single bounded line).

    Comment lines are ignored by conforming SSE clients, so a status frame can
    never be parsed as a data event; control characters are dropped so ``text``
    cannot end the comment early or inject a field.
    """
    line = " ".join(
        "".join(ch if ch.isprintable() else " " for ch in str(text)).split()
    )[:_MAX_STATUS_CHARS]
    return (STATUS_FRAME_PREFIX + line + "\n\n").encode("utf-8")


class SSEKeepAlive:
    """Background writer of SSE comment frames until stopped.

    ``write`` sends bytes to the client and raises ``OSError`` once the client
    has gone.  Every write, including the caller's own after ``stop``, must go
    through ``lock`` so frames never interleave.
    """

    def __init__(self, write: Callable[[bytes], None], interval_seconds: float,
                 *, thread_factory: Callable[..., object],
                 status: Callable[[], str | None] | None = None):
        # ``thread_factory`` is the runtime's owned-thread constructor, injected
        # by the serving entry point (interfaces may not import the platform).
        self._thread_factory = thread_factory
        if interval_seconds <= 0:
            raise ValueError("keep-alive interval must be positive")
        self._write = write
        # Returns a short wait description (``queued for <model> (position
        # k/N)``) while the turn waits for a model slot, else ``None``; sent
        # as a status comment frame instead of the plain keep-alive.
        self._status = status
        self._interval = float(interval_seconds)
        self._stop = threading.Event()
        self.lock = threading.Lock()
        self.client_gone = False
        # The ``chatcmpl-`` id of the completion this response carries, once a
        # frame has used one (``live_stream.arm``); later frames reuse it.
        self.completion_id: str | None = None
        self._thread: threading.Thread | None = None
        # True while the client may be showing a status line we sent.
        self._status_shown = False

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            with self.lock:
                if self._stop.is_set():
                    return
                frame = KEEPALIVE_FRAME
                shown = False
                if self._status is not None:
                    try:
                        text = self._status()
                    except Exception:  # a status probe must never end the stream
                        text = None
                    if text:
                        frame = status_frame(text)
                        shown = True
                    elif self._status_shown:
                        frame = STATUS_CLEAR_FRAME
                try:
                    self._write(frame)
                except (OSError, ValueError):
                    self.client_gone = True
                    return
                self._status_shown = shown

    def start(self) -> "SSEKeepAlive":
        self._thread = self._thread_factory(
            target=self._run, name="sonder-sse-keepalive", daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> bool:
        """Stop the writer; ``True`` while the client is still connected."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
        if self._status_shown and not self.client_gone:
            with self.lock:
                try:
                    self._write(STATUS_CLEAR_FRAME)
                except (OSError, ValueError):
                    self.client_gone = True
            self._status_shown = False
        return not self.client_gone
