"""Thread-safe parent/child cancellation and quiescence primitives."""
from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from enum import Enum
from typing import Iterator


class CancellationStatus(str, Enum):
    ACTIVE = "active"
    CANCEL_REQUESTED = "cancel_requested"
    QUIESCENT = "quiescent"


@dataclass(frozen=True)
class CancellationSnapshot:
    node_id: str
    status: CancellationStatus
    reason: str | None
    active_leases: int


class CancellationLease:
    """A counted unit of work that must leave before its node is quiescent."""

    def __init__(self, node: "CancellationNode") -> None:
        self._node = node
        self._released = False

    @property
    def cancelled(self) -> bool:
        return self._node.cancelled

    def release(self) -> None:
        if not self._released:
            self._released = True
            self._node._release_lease()

    def __enter__(self) -> "CancellationLease":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()


class CancellationNode:
    """A cancellation scope whose request propagates to all descendants.

    Cancellation is cooperative: requesting it signals the node immediately;
    quiescence is reached only after every acquired lease in the subtree is
    released. The first cancellation reason is retained for stable reporting.
    """

    def __init__(self, *, node_id: str | None = None, parent: "CancellationNode | None" = None) -> None:
        self.node_id = node_id or uuid.uuid4().hex
        self._parent = parent
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._cancel_event = threading.Event()
        self._children: dict[str, CancellationNode] = {}
        self._active_leases = 0
        self._status = CancellationStatus.ACTIVE
        self._reason: str | None = None
        if parent is not None:
            parent._add_child(self)

    def _add_child(self, child: "CancellationNode") -> None:
        with self._condition:
            if child.node_id in self._children:
                raise ValueError(f"duplicate cancellation node id: {child.node_id}")
            self._children[child.node_id] = child
            inherited_reason = self._reason if self._cancel_event.is_set() else None
        if inherited_reason is not None:
            child.cancel(reason=inherited_reason)

    def child(self, *, node_id: str | None = None) -> "CancellationNode":
        return CancellationNode(node_id=node_id, parent=self)

    @property
    def cancelled(self) -> bool:
        return self._cancel_event.is_set()

    @property
    def status(self) -> CancellationStatus:
        with self._lock:
            return self._status

    @property
    def reason(self) -> str | None:
        with self._lock:
            return self._reason

    def snapshot(self) -> CancellationSnapshot:
        with self._lock:
            return CancellationSnapshot(self.node_id, self._status, self._reason, self._active_leases)

    def cancel(self, *, reason: str = "cancellation requested") -> bool:
        """Request cancellation, returning whether this call changed state."""
        with self._condition:
            if self._cancel_event.is_set():
                return False
            self._reason = reason
            self._status = CancellationStatus.CANCEL_REQUESTED
            self._cancel_event.set()
            children = tuple(self._children.values())
            self._condition.notify_all()
        for child in children:
            child.cancel(reason=reason)
        return True

    def acquire(self) -> CancellationLease:
        with self._condition:
            self._active_leases += 1
        return CancellationLease(self)

    def _release_lease(self) -> None:
        with self._condition:
            if self._active_leases == 0:
                raise RuntimeError("cancellation lease released more than once")
            self._active_leases -= 1
            self._condition.notify_all()
        self._refresh_quiescence()

    def _refresh_quiescence(self, *, propagate: bool = True) -> None:
        with self._lock:
            children = tuple(self._children.values())
        for child in children:
            child._refresh_quiescence(propagate=False)
        with self._condition:
            if self._cancel_event.is_set() and self._active_leases == 0 and all(
                child.status is CancellationStatus.QUIESCENT for child in self._children.values()
            ):
                self._status = CancellationStatus.QUIESCENT
                self._condition.notify_all()
            parent = self._parent
        if propagate and parent is not None:
            parent._refresh_quiescence()

    def join(self, timeout: float | None = None) -> bool:
        """Wait until this cancelled subtree is quiescent."""
        deadline = None if timeout is None else time.monotonic() + max(timeout, 0.0)
        self._refresh_quiescence()
        with self._condition:
            while self._status is not CancellationStatus.QUIESCENT:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._condition.wait(remaining)
                self._refresh_quiescence()
            return True

    def wait(self, timeout: float | None = None) -> bool:
        """Wait for the cancellation request; return whether it was requested."""
        return self._cancel_event.wait(timeout)

    def children(self) -> Iterator["CancellationNode"]:
        with self._lock:
            yield from tuple(self._children.values())


__all__ = ["CancellationLease", "CancellationNode", "CancellationSnapshot", "CancellationStatus"]
