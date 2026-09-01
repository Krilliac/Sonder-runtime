"""Application ports for compute-node observations and remote jobs."""
from __future__ import annotations

from datetime import datetime
from typing import Protocol, TYPE_CHECKING

from ...domain.compute_fabric import ComputeNode, NodeSnapshot

if TYPE_CHECKING:
    from ..compute_fabric.jobs import RemoteJobEnvelope, RemoteJobReceipt


class ComputeSnapshotSource(Protocol):
    def snapshot(self, node: ComputeNode, *, now: datetime) -> NodeSnapshot: ...


class ComputeNodeRegistryPort(Protocol):
    def get_node(self, node_id: str) -> ComputeNode: ...

    def observe(self, snapshot: NodeSnapshot) -> NodeSnapshot: ...

    def list_snapshots(self, *, now: datetime) -> tuple[NodeSnapshot, ...]: ...


class ComputeRemoteJobTransport(Protocol):
    def submit(self, node: ComputeNode, envelope: "RemoteJobEnvelope") -> "RemoteJobReceipt": ...

    def status(self, node: ComputeNode, remote_job_id: str) -> "RemoteJobReceipt": ...

    def cancel(
        self, node: ComputeNode, remote_job_id: str, *, reason: str
    ) -> "RemoteJobReceipt": ...


__all__ = [
    "ComputeNodeRegistryPort",
    "ComputeRemoteJobTransport",
    "ComputeSnapshotSource",
]
