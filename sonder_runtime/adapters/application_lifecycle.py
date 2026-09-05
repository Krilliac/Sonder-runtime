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
        self._factory_used = False
        self._owned = False
        self._terminal = False

    def get(self) -> ApplicationT:
        with self._lock:
            if self._terminal:
                raise RuntimeError("owned application admission is terminal")
            if self._application is None:
                self._factory_used = True
                self._application = self._factory()
            return self._application

    def reset(self) -> None:
        with self._lock:
            if self._owned:
                raise RuntimeError("owned application cannot be reset or replaced")
            self._application = None

    def install_owned(self, application: ApplicationT) -> None:
        with self._lock:
            if self._factory_used or self._application is not None or self._owned or application is None:
                raise RuntimeError("owned application requires unused lifecycle composition")
            self._application = application
            self._owned = True

    def stop_owned(self, application: ApplicationT) -> None:
        with self._lock:
            if not self._owned or self._application is not application:
                raise RuntimeError("exact owned application required")
            self._terminal = True
