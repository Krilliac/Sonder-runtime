"""Thread-safe ownership of the process-wide application instance.

The composition root supplies the factory; this adapter owns only the
singleton lifecycle and stays independent of the application graph.
"""
from __future__ import annotations

from collections.abc import Callable
from threading import Lock
from typing import Generic, TypeVar


ApplicationT = TypeVar("ApplicationT")


class ApplicationLifecycle(Generic[ApplicationT]):
    """Lazily build and reset one process-wide application instance."""

    def __init__(self, factory: Callable[[], ApplicationT]) -> None:
        self._factory = factory
        self._application: ApplicationT | None = None
        self._lock = Lock()

    def get(self) -> ApplicationT:
        with self._lock:
            if self._application is None:
                self._application = self._factory()
            return self._application

    def reset(self) -> None:
        with self._lock:
            self._application = None
