"""Work-stealing task scheduler.

Each worker owns a double-ended queue.  Workers push and pop from
their own deque (LIFO for cache locality).  When a worker's deque is
empty, it steals from another worker's deque (FIFO end -- oldest
tasks, least likely to share cache lines with the victim).

No I/O -- callers own threading.  The deques themselves are safe for
single-producer operations; cross-deque stealing is the caller's
responsibility to synchronize.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class StealResult(Generic[T]):
    item: T | None
    source_worker: str
    stolen: bool


class WorkerDeque(Generic[T]):
    """Per-worker double-ended queue for work-stealing."""

    def __init__(self, worker_id: str, max_size: int = 0) -> None:
        self._worker_id = worker_id
        self._deque: deque[T] = deque(maxlen=max_size if max_size > 0 else None)
        self._completed = 0
        self._stolen_from = 0

    def push(self, item: T) -> bool:
        if self._deque.maxlen and len(self._deque) >= self._deque.maxlen:
            return False
        self._deque.append(item)
        return True

    def pop(self) -> T | None:
        try:
            return self._deque.pop()
        except IndexError:
            return None

    def steal(self) -> T | None:
        try:
            item = self._deque.popleft()
            self._stolen_from += 1
            return item
        except IndexError:
            return None

    def mark_completed(self) -> None:
        self._completed += 1

    @property
    def worker_id(self) -> str:
        return self._worker_id

    @property
    def size(self) -> int:
        return len(self._deque)

    @property
    def empty(self) -> bool:
        return len(self._deque) == 0

    @property
    def completed(self) -> int:
        return self._completed

    @property
    def stolen_from(self) -> int:
        return self._stolen_from


class WorkStealingScheduler(Generic[T]):
    """Coordinates work-stealing across a pool of worker deques."""

    def __init__(self) -> None:
        self._workers: dict[str, WorkerDeque[T]] = {}
        self._total_submitted = 0
        self._total_steals = 0

    def add_worker(self, worker_id: str, max_queue: int = 0) -> WorkerDeque[T]:
        if worker_id in self._workers:
            return self._workers[worker_id]
        dq: WorkerDeque[T] = WorkerDeque(worker_id, max_size=max_queue)
        self._workers[worker_id] = dq
        return dq

    def remove_worker(self, worker_id: str) -> list[T]:
        dq = self._workers.pop(worker_id, None)
        if dq is None:
            return []
        orphaned: list[T] = []
        while not dq.empty:
            item = dq.pop()
            if item is not None:
                orphaned.append(item)
        return orphaned

    def submit(self, worker_id: str, item: T) -> bool:
        dq = self._workers.get(worker_id)
        if dq is None:
            return False
        ok = dq.push(item)
        if ok:
            self._total_submitted += 1
        return ok

    def try_steal(self, thief_id: str) -> StealResult[T]:
        thief = self._workers.get(thief_id)
        if thief is None:
            return StealResult(item=None, source_worker="", stolen=False)

        best_victim: WorkerDeque[T] | None = None
        best_size = 0
        for wid, dq in self._workers.items():
            if wid == thief_id:
                continue
            if dq.size > best_size:
                best_size = dq.size
                best_victim = dq

        if best_victim is None or best_victim.empty:
            return StealResult(item=None, source_worker="", stolen=False)

        item = best_victim.steal()
        if item is not None:
            self._total_steals += 1
            return StealResult(item=item, source_worker=best_victim.worker_id, stolen=True)

        return StealResult(item=None, source_worker="", stolen=False)

    def get_work(self, worker_id: str) -> StealResult[T]:
        dq = self._workers.get(worker_id)
        if dq is None:
            return StealResult(item=None, source_worker="", stolen=False)

        item = dq.pop()
        if item is not None:
            return StealResult(item=item, source_worker=worker_id, stolen=False)

        return self.try_steal(worker_id)

    @property
    def total_submitted(self) -> int:
        return self._total_submitted

    @property
    def total_steals(self) -> int:
        return self._total_steals

    @property
    def worker_count(self) -> int:
        return len(self._workers)

    def snapshot(self) -> dict[str, Any]:
        return {
            "workers": {
                wid: {
                    "queue_size": dq.size,
                    "completed": dq.completed,
                    "stolen_from": dq.stolen_from,
                }
                for wid, dq in self._workers.items()
            },
            "total_submitted": self._total_submitted,
            "total_steals": self._total_steals,
        }
