"""Bounded, deterministic placement admission for whole-job requests.

This queue is intentionally process-local.  Durable job state remains the
source of truth for work that has been admitted to a worker.  The queue only
controls a bounded pre-placement cohort and gives callers a stable explanation
for ordering, duplicate replay, and pressure refusals.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import re
from threading import RLock

from ...domain.common.errors import Conflict
from ...domain.compute_fabric import WorkloadRequest


_SHA256 = re.compile(r"[0-9a-f]{64}")
_MAX_QUEUE_DEPTH = 4096
_MAX_EXPIRE_ROWS = 1024


def _timestamp(value: datetime | None, label: str) -> datetime:
    current = datetime.now(timezone.utc) if value is None else value
    if not isinstance(current, datetime) or current.tzinfo is None or current.utcoffset() is None:
        raise ValueError(f"{label} must be timezone-aware")
    return current.astimezone(timezone.utc)


@dataclass(frozen=True, slots=True)
class PlacementQueueItem:
    request_id: str
    request_digest: str
    priority: int
    enqueued_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.request_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", self.request_id
        ):
            raise ValueError("placement queue request_id must be a bounded identity")
        if not isinstance(self.request_digest, str) or _SHA256.fullmatch(self.request_digest) is None:
            raise ValueError("placement queue request_digest must be SHA-256")
        if isinstance(self.priority, bool) or not isinstance(self.priority, int) or not -100 <= self.priority <= 100:
            raise ValueError("placement queue priority must be within -100..100")
        object.__setattr__(self, "enqueued_at", _timestamp(self.enqueued_at, "enqueued_at"))


@dataclass(frozen=True, slots=True)
class QueueAdmission:
    request_id: str
    admitted: bool
    reason_code: str
    depth: int
    position: int | None
    priority: int


@dataclass(frozen=True, slots=True)
class QueueExplanation:
    request_id: str
    reason_code: str
    message: str
    depth: int
    position: int | None
    priority: int | None


class PlacementQueue:
    """Thread-safe bounded queue ordered by priority, age, and request ID."""

    def __init__(self, *, max_depth: int = 32) -> None:
        if type(max_depth) is not int or not 1 <= max_depth <= _MAX_QUEUE_DEPTH:
            raise ValueError(f"placement queue max_depth must be within 1..{_MAX_QUEUE_DEPTH}")
        self.max_depth = max_depth
        self._items: dict[str, PlacementQueueItem] = {}
        self._lock = RLock()

    @staticmethod
    def _rank(item: PlacementQueueItem):
        return (-item.priority, item.enqueued_at, item.request_id)

    def _ordered_locked(self) -> tuple[PlacementQueueItem, ...]:
        return tuple(sorted(self._items.values(), key=self._rank))

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._items)

    def snapshot(self) -> tuple[PlacementQueueItem, ...]:
        with self._lock:
            return self._ordered_locked()

    def enqueue(self, request: WorkloadRequest, *, now: datetime | None = None) -> QueueAdmission:
        if not isinstance(request, WorkloadRequest):
            raise TypeError("placement queue accepts WorkloadRequest values")
        item = PlacementQueueItem(
            request_id=request.request_id,
            request_digest=request.digest(),
            priority=request.priority,
            enqueued_at=_timestamp(now, "now"),
        )
        with self._lock:
            existing = self._items.get(item.request_id)
            if existing is not None:
                if existing.request_digest != item.request_digest:
                    raise Conflict("placement queue identity is already bound to another request")
                position = self._ordered_locked().index(existing) + 1
                return QueueAdmission(
                    item.request_id, True, "already_queued", len(self._items), position, existing.priority
                )
            if len(self._items) >= self.max_depth:
                return QueueAdmission(item.request_id, False, "queue_full", len(self._items), None, item.priority)
            self._items[item.request_id] = item
            position = self._ordered_locked().index(item) + 1
            return QueueAdmission(item.request_id, True, "queued", len(self._items), position, item.priority)

    def claim(self, limit: int = 1) -> tuple[PlacementQueueItem, ...]:
        if type(limit) is not int or not 1 <= limit <= self.max_depth:
            raise ValueError("placement queue claim limit is outside the queue bound")
        with self._lock:
            selected = self._ordered_locked()[:limit]
            for item in selected:
                del self._items[item.request_id]
            return selected

    def remove(self, request_id: str) -> bool:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("placement queue request_id must be non-empty")
        with self._lock:
            return self._items.pop(request_id, None) is not None

    def explain(self, request_id: str) -> QueueExplanation:
        if not isinstance(request_id, str) or not request_id:
            raise ValueError("placement queue request_id must be non-empty")
        with self._lock:
            ordered = self._ordered_locked()
            item = self._items.get(request_id)
            if item is None:
                return QueueExplanation(
                    request_id, "not_queued", "request is not waiting for placement", len(ordered), None, None
                )
            position = ordered.index(item) + 1
            ahead = position - 1
            message = f"queued at priority {item.priority}; {ahead} request(s) ahead"
            return QueueExplanation(request_id, "queued", message, len(ordered), position, item.priority)

    def expire(
        self,
        *,
        now: datetime | None = None,
        max_wait_seconds: int,
        limit: int = _MAX_EXPIRE_ROWS,
    ) -> tuple[QueueExplanation, ...]:
        if type(max_wait_seconds) is not int or not 1 <= max_wait_seconds <= 86_400:
            raise ValueError("placement queue max_wait_seconds must be within 1..86400")
        if type(limit) is not int or not 1 <= limit <= _MAX_EXPIRE_ROWS:
            raise ValueError(f"placement queue expire limit must be within 1..{_MAX_EXPIRE_ROWS}")
        current = _timestamp(now, "now")
        with self._lock:
            ordered = self._ordered_locked()
            expired = tuple(
                item for item in ordered
                if (current - item.enqueued_at).total_seconds() >= max_wait_seconds
            )[:limit]
            positions = {item.request_id: index + 1 for index, item in enumerate(ordered)}
            for item in expired:
                self._items.pop(item.request_id, None)
            return tuple(
                QueueExplanation(
                    item.request_id,
                    "queue_expired",
                    f"queue wait exceeded {max_wait_seconds} second(s)",
                    len(ordered),
                    positions[item.request_id],
                    item.priority,
                )
                for item in expired
            )


__all__ = [
    "PlacementQueue",
    "PlacementQueueItem",
    "QueueAdmission",
    "QueueExplanation",
]
