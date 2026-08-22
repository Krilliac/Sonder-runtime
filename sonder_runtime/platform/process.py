"""Process-wide cooperative cancellation primitives."""
from __future__ import annotations

import threading


class CancellationToken:
    """Thread-safe cancellation signal shared across process lifecycle work."""

    def __init__(self) -> None:
        self._event = threading.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    def wait(self, timeout: float | None = None) -> bool:
        return self._event.wait(timeout)


__all__ = ["CancellationToken"]
