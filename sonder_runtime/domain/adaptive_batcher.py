"""Adaptive request batching.

Collects individual requests into batches, dispatching when either the
batch reaches a size threshold or a time window expires.  The window
adapts based on a pressure signal: wider at low load (accumulate
larger batches for throughput) and narrower at high load (minimize
latency).

No I/O, no background threads -- callers drive the clock and flush.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class BatchConfig:
    max_batch_size: int = 8
    min_window_ms: float = 5.0
    max_window_ms: float = 50.0
    pressure_alpha: float = 0.3


@dataclass(frozen=True, slots=True)
class Batch(Generic[T]):
    items: tuple[T, ...]
    size: int
    wait_ms: float
    pressure: float


class AdaptiveBatcher(Generic[T]):
    """Collects items into pressure-adaptive batches.

    ``add()`` enqueues an item.  ``flush()`` returns the current batch
    if the size or time threshold is met.  ``should_flush()`` checks
    thresholds without consuming.
    """

    def __init__(self, config: BatchConfig | None = None) -> None:
        self._cfg = config or BatchConfig()
        self._buffer: list[T] = []
        self._batch_start: float | None = None
        self._pressure = 0.0
        self._batches_emitted = 0
        self._items_batched = 0

    def add(self, item: T, *, now: float | None = None) -> Batch[T] | None:
        ts = now if now is not None else time.monotonic()
        if self._batch_start is None:
            self._batch_start = ts
        self._buffer.append(item)

        if len(self._buffer) >= self._cfg.max_batch_size:
            return self._emit(ts)
        return None

    def should_flush(self, *, now: float | None = None) -> bool:
        if not self._buffer:
            return False
        if len(self._buffer) >= self._cfg.max_batch_size:
            return True
        ts = now if now is not None else time.monotonic()
        return self._elapsed_ms(ts) >= self._current_window_ms()

    def flush(self, *, now: float | None = None) -> Batch[T] | None:
        if not self._buffer:
            return None
        ts = now if now is not None else time.monotonic()
        if len(self._buffer) >= self._cfg.max_batch_size or self._elapsed_ms(ts) >= self._current_window_ms():
            return self._emit(ts)
        return None

    def force_flush(self, *, now: float | None = None) -> Batch[T] | None:
        if not self._buffer:
            return None
        ts = now if now is not None else time.monotonic()
        return self._emit(ts)

    def update_pressure(self, pressure: float) -> None:
        clamped = max(0.0, min(1.0, pressure))
        alpha = self._cfg.pressure_alpha
        self._pressure = alpha * clamped + (1.0 - alpha) * self._pressure

    def _current_window_ms(self) -> float:
        span = self._cfg.max_window_ms - self._cfg.min_window_ms
        return self._cfg.max_window_ms - (self._pressure * span)

    def _elapsed_ms(self, now: float) -> float:
        if self._batch_start is None:
            return 0.0
        return (now - self._batch_start) * 1000.0

    def _emit(self, now: float) -> Batch[T]:
        items = tuple(self._buffer)
        wait = self._elapsed_ms(now)
        self._buffer.clear()
        self._batches_emitted += 1
        self._items_batched += len(items)
        batch = Batch(
            items=items,
            size=len(items),
            wait_ms=round(wait, 3),
            pressure=round(self._pressure, 4),
        )
        self._batch_start = None
        return batch

    @property
    def pending(self) -> int:
        return len(self._buffer)

    @property
    def batches_emitted(self) -> int:
        return self._batches_emitted

    @property
    def items_batched(self) -> int:
        return self._items_batched

    @property
    def current_window_ms(self) -> float:
        return self._current_window_ms()
