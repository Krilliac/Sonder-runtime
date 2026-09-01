from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.application.compute_fabric.registry import ComputeNodeRegistry
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
    WorkloadKind,
)


NOW = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)


def _node(node_id: str = "node") -> ComputeNode:
    return ComputeNode(
        node_id=node_id,
        origin=f"https://{node_id}:8443",
        local=False,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
        configured_capabilities=frozenset({ComputeCapability.CPU, ComputeCapability.CMAKE}),
        workspace_mappings=frozenset({"sonder"}),
    )


def _snapshot(node: ComputeNode) -> NodeSnapshot:
    return NodeSnapshot(
        node=node,
        observed_at=NOW,
        health=NodeHealth.HEALTHY,
        live_capabilities=frozenset({ComputeCapability.CPU, ComputeCapability.CMAKE}),
        advertised_workloads=frozenset({WorkloadKind.BUILD}),
        resources=NodeResources(free_ram_bytes=8 << 30),
    )


def test_registry_telemetry_cannot_widen_static_authority() -> None:
    configured = _node()
    registry = ComputeNodeRegistry((configured,), snapshot_ttl=timedelta(seconds=30))
    advertised_node = replace(
        configured,
        allowed_workloads=frozenset({WorkloadKind.BUILD, WorkloadKind.SERVICE}),
        configured_capabilities=frozenset(
            {ComputeCapability.CPU, ComputeCapability.CMAKE, ComputeCapability.DOCKER}
        ),
    )
    advertised = replace(
        _snapshot(advertised_node),
        advertised_workloads=frozenset({WorkloadKind.BUILD, WorkloadKind.SERVICE}),
        live_capabilities=frozenset(
            {ComputeCapability.CPU, ComputeCapability.CMAKE, ComputeCapability.DOCKER}
        ),
    )
    registry.observe(advertised)
    effective = registry.list_snapshots(now=NOW)[0]
    assert effective.node is configured
    assert effective.effective_workloads == frozenset({WorkloadKind.BUILD})
    assert effective.effective_capabilities == frozenset(
        {ComputeCapability.CPU, ComputeCapability.CMAKE}
    )


def test_registry_rejects_unknown_nodes_and_conflicting_origins() -> None:
    registry = ComputeNodeRegistry((_node("known"),))
    with pytest.raises(KeyError, match="not configured"):
        registry.observe(_snapshot(_node("unknown")))
    conflicting = replace(_node("known"), origin="https://other:8443")
    with pytest.raises(ValueError, match="origin"):
        registry.observe(_snapshot(conflicting))


def test_registry_is_deterministic_and_marks_staleness_without_discarding_evidence() -> None:
    registry = ComputeNodeRegistry(
        (_node("node-b"), _node("node-a")),
        snapshot_ttl=timedelta(seconds=30),
    )
    registry.observe(_snapshot(_node("node-b")))
    registry.observe(replace(_snapshot(_node("node-a")), observed_at=NOW - timedelta(seconds=31)))
    snapshots = registry.list_snapshots(now=NOW)
    assert tuple(item.node.node_id for item in snapshots) == ("node-a", "node-b")
    assert registry.is_stale("node-a", now=NOW)
    assert not registry.is_stale("node-b", now=NOW)
    assert registry.last_observation("node-a") is not None


def test_probe_failure_preserves_evidence_and_worker_clock_cannot_poison_recovery() -> None:
    node = _node()
    registry = ComputeNodeRegistry((node,), snapshot_ttl=timedelta(seconds=30))
    initial = replace(
        _snapshot(node),
        observed_at=NOW + timedelta(seconds=4),
        evidence_ref="worker-snapshot:sha256:abc",
    )
    registry.observe(initial, received_at=NOW)

    failed = registry.mark_probe_failed(
        node.node_id,
        received_at=NOW + timedelta(seconds=10),
        evidence_ref="probe-failed:TimeoutError",
    )
    assert failed.health is NodeHealth.UNHEALTHY
    assert failed.resources == initial.resources
    assert failed.live_capabilities == initial.live_capabilities
    assert failed.evidence_ref == initial.evidence_ref
    assert failed.observed_at == initial.observed_at
    assert failed.received_at == NOW + timedelta(seconds=10)
    assert registry.last_probe_error(node.node_id) == "probe-failed:TimeoutError"

    recovered = replace(
        initial,
        observed_at=NOW + timedelta(seconds=1),
        health=NodeHealth.HEALTHY,
        evidence_ref="worker-snapshot:sha256:def",
    )
    accepted = registry.observe(
        recovered,
        received_at=NOW + timedelta(seconds=20),
    )
    assert accepted.health is NodeHealth.HEALTHY
    assert accepted.evidence_ref == "worker-snapshot:sha256:def"
    assert accepted.received_at == NOW + timedelta(seconds=20)
    assert registry.last_probe_error(node.node_id) is None


def test_registry_requires_unique_configured_node_ids() -> None:
    with pytest.raises(ValueError, match="duplicate"):
        ComputeNodeRegistry((_node("same"), _node("same")))
