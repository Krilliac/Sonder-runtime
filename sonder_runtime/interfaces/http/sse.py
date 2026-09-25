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


class SSEKeepAlive:
    """Background writer of SSE comment frames until stopped.

    ``write`` sends bytes to the client and raises ``OSError`` once the client
    has gone.  Every write, including the caller's own after ``stop``, must go
    through ``lock`` so frames never interleave.
    """

    def __init__(self, write: Callable[[bytes], None], interval_seconds: float):
        if interval_seconds <= 0:
            raise ValueError("keep-alive interval must be positive")
        self._write = write
        self._interval = float(interval_seconds)
        self._stop = threading.Event()
        self.lock = threading.Lock()
        self.client_gone = False
        self._thread: threading.Thread | None = None

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            with self.lock:
                if self._stop.is_set():
                    return
                try:
                    self._write(KEEPALIVE_FRAME)
                except (OSError, ValueError):
                    self.client_gone = True
                    return

    def start(self) -> "SSEKeepAlive":
        self._thread = threading.Thread(
            target=self._run, name="sonder-sse-keepalive", daemon=True,
        )
        self._thread.start()
        return self

    def stop(self) -> bool:
        """Stop the writer; ``True`` while the client is still connected."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)
        return not self.client_gone
