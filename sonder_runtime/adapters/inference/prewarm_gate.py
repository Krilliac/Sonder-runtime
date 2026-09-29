"""One speculative model load per model, and a bounded wait for it.

A prewarm is an ordinary model POST, so with a worker pool it holds an
admission slot on the worker that serves the model. With the default of one
in-flight request per worker, the real request for the same model then waited
only the short admission timeout and failed with backpressure while the
prewarm it was meant to benefit still held the slot. A real request now waits,
within its own deadline, for an in-flight prewarm of the same model before it
asks the pool for admission; the load it waits for is the one it needs anyway.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, TypeVar

_T = TypeVar("_T")
_condition = threading.Condition()
_inflight: set[str] = set()
_local = threading.local()


def begin(model: str) -> bool:
    """Claim the single in-flight prewarm for ``model``; False if one runs."""
    with _condition:
        if model in _inflight:
            return False
        _inflight.add(model)
        return True


def finish(model: str) -> None:
    with _condition:
        _inflight.discard(model)
        _condition.notify_all()


def run_as_prewarm(load: Callable[[], _T]) -> _T:
    """Run ``load`` marked as the prewarm so it never waits for itself."""
    _local.active = True
    try:
        return load()
    finally:
        _local.active = False


def await_prewarm(model: object, timeout: float) -> bool:
    """Wait at most ``timeout`` seconds for an in-flight prewarm of ``model``.

    Returns False when the wait expired with the prewarm still running; the
    caller then proceeds and pays the ordinary admission behaviour.
    """
    if not isinstance(model, str) or not model or getattr(_local, "active", False):
        return True
    deadline = time.monotonic() + max(0.0, float(timeout))
    with _condition:
        while model in _inflight:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _condition.wait(remaining)
    return True
