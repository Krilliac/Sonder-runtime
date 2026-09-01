"""Application ports for compute-node observations and remote jobs."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol

from ...domain.compute_fabric import ComputeNode, NodeSnapshot


class ComputeSnapshotSource(Protocol):
    def snapshot(self, node: ComputeNode, *, now: datetime) -> NodeSnapshot: ...


class ComputeNodeRegistryPort(Protocol):
    def get_node(self, node_id: str) -> ComputeNode: ...

    def observe(self, snapshot: NodeSnapshot) -> NodeSnapshot: ...

    def list_snapshots(self, *, now: datetime) -> tuple[NodeSnapshot, ...]: ...


__all__ = ["ComputeNodeRegistryPort", "ComputeSnapshotSource"]
