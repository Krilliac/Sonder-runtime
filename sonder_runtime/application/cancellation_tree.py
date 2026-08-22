"""Application service for managing a cancellation scope tree."""
from __future__ import annotations

from sonder_runtime.domain.cancellation_tree import (
    CancellationLease,
    CancellationNode,
    CancellationSnapshot,
)


class CancellationTree:
    """Own the root scope and provide node-id based application operations."""

    def __init__(self, *, root_id: str = "root") -> None:
        self.root = CancellationNode(node_id=root_id)
        self._nodes = {self.root.node_id: self.root}

    def create_child(self, parent_id: str = "root", *, node_id: str | None = None) -> CancellationNode:
        parent = self.node(parent_id)
        if node_id is not None and node_id in self._nodes:
            raise ValueError(f"duplicate cancellation node id: {node_id}")
        child = parent.child(node_id=node_id)
        self._nodes[child.node_id] = child
        return child

    def node(self, node_id: str) -> CancellationNode:
        try:
            return self._nodes[node_id]
        except KeyError as exc:
            raise KeyError(f"unknown cancellation node: {node_id}") from exc

    def cancel(self, node_id: str = "root", *, reason: str = "cancellation requested") -> bool:
        return self.node(node_id).cancel(reason=reason)

    def acquire(self, node_id: str = "root") -> CancellationLease:
        return self.node(node_id).acquire()

    def status(self, node_id: str = "root") -> CancellationSnapshot:
        return self.node(node_id).snapshot()

    def join(self, node_id: str = "root", timeout: float | None = None) -> bool:
        return self.node(node_id).join(timeout)


__all__ = ["CancellationTree"]
