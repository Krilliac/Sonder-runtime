from __future__ import annotations

from datetime import datetime, timedelta, timezone
from threading import Barrier

from sonder_runtime.application.compute_fabric.refresh import refresh_remote_snapshots
from sonder_runtime.application.compute_fabric.registry import ComputeNodeRegistry
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    NodeSnapshot,
    WorkloadKind,
)


NOW = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)


def _node(node_id: str) -> ComputeNode:
    return ComputeNode(
        node_id=node_id,
        origin=f"https://{node_id}:8443",
        local=False,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
        configured_capabilities=frozenset({ComputeCapability.CPU}),
    )


def test_remote_snapshot_refresh_probes_nodes_concurrently() -> None:
    nodes = (_node("node-a"), _node("node-b"))
    registry = ComputeNodeRegistry(nodes)
    rendezvous = Barrier(2)

    class Source:
        def snapshot(self, node, *, now):
            rendezvous.wait(timeout=2)
            return NodeSnapshot(
                node=node,
                observed_at=now,
                health=NodeHealth.HEALTHY,
                live_capabilities=node.configured_capabilities,
                advertised_workloads=node.allowed_workloads,
            )

    refresh_remote_snapshots(
        registry,
        Source(),
        now=lambda: NOW,
        max_workers=2,
    )

    assert tuple(
        snapshot.node.node_id for snapshot in registry.list_snapshots(now=NOW)
    ) == ("node-a", "node-b")
    assert all(
        snapshot.health is NodeHealth.HEALTHY
        for snapshot in registry.list_snapshots(now=NOW)
    )


def test_remote_snapshot_receipt_time_is_captured_after_probe_completion() -> None:
    node = _node("node-a")
    registry = ComputeNodeRegistry((node,))
    instants = iter((NOW, NOW + timedelta(seconds=4)))

    class Source:
        def snapshot(self, candidate, *, now):
            return NodeSnapshot(
                node=candidate,
                observed_at=now,
                health=NodeHealth.HEALTHY,
                live_capabilities=candidate.configured_capabilities,
                advertised_workloads=candidate.allowed_workloads,
            )

    refresh_remote_snapshots(
        registry,
        Source(),
        now=lambda: next(instants),
        max_workers=1,
    )

    snapshot = registry.last_observation("node-a")
    assert snapshot is not None
    assert snapshot.observed_at == NOW
    assert snapshot.received_at == NOW + timedelta(seconds=4)
