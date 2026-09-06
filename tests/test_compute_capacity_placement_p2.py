from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.adapters.persistence.sqlite.job_registry import SQLiteDurableJobRegistry
from sonder_runtime.domain.common.errors import CapacityExceeded, Conflict
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    ComputePlacementScheduler,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
    PlacementPolicy,
    WorkloadKind,
    WorkloadRequest,
)
from sonder_runtime.application.compute_fabric.placement_queue import PlacementQueue


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)


def _budget(host="worker", fingerprint=None, memory=100, max_jobs=2):
    from sonder_runtime.application.compute_fabric.capacity import WorkerBudget

    return WorkerBudget(host, memory, max_jobs, fingerprint)


def _request(request_id="request", **changes):
    return replace(
        WorkloadRequest(
            request_id=request_id,
            kind=WorkloadKind.BUILD,
            required_capabilities=frozenset({ComputeCapability.CPU}),
            allow_remote=True,
            placement_policy=PlacementPolicy.RANK_ALL,
        ),
        **changes,
    )


def _snapshot(*, active_jobs=0):
    node = ComputeNode(
        node_id="worker",
        origin=None,
        local=True,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
        configured_capabilities=frozenset({ComputeCapability.CPU}),
    )
    return NodeSnapshot(
        node=node,
        observed_at=NOW,
        received_at=NOW,
        health=NodeHealth.HEALTHY,
        live_capabilities=frozenset({ComputeCapability.CPU}),
        advertised_workloads=frozenset({WorkloadKind.BUILD}),
        resources=NodeResources(cpu_count=4, free_ram_bytes=100),
        active_jobs=active_jobs,
    )


def test_same_logical_host_label_cannot_cross_physical_fence(tmp_path):
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    first = _budget(fingerprint="a" * 64)
    second = _budget(fingerprint="b" * 64)
    lease = registry.reserve_capacity(first, "one", "a" * 64, 100)
    registry.dispatch_capacity(lease.job_id, lease.token)

    with pytest.raises(Conflict, match="physical host identity"):
        registry.reserve_capacity(second, "two", "b" * 64, 100)


def test_default_physical_identity_is_opaque_and_reservation_listing_redacts_token(tmp_path):
    from sonder_runtime.adapters.persistence.sqlite.physical_identity import physical_host_identity

    identity = physical_host_identity("worker")
    assert identity.authority_id == "worker"
    assert len(identity.fingerprint) == 64
    assert identity.fingerprint.islower() and all(char in "0123456789abcdef" for char in identity.fingerprint)
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db")
    lease = registry.reserve_capacity(_budget(), "one", "a" * 64, 1)
    view = registry.list_capacity()[0]
    assert view.job_id == lease.job_id
    assert view.physical_host_fingerprint == lease.physical_host_fingerprint
    assert not hasattr(view, "token")


def test_capacity_schema_migration_adds_fence_and_release_columns(tmp_path):
    import sqlite3
    from sonder_runtime.adapters.persistence.sqlite.job_registry import initialize_schema

    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    initialize_schema(connection)
    connection.executescript(
        """
        CREATE TABLE worker_capacity_budget (
            host_id TEXT PRIMARY KEY, memory_bytes INTEGER NOT NULL, max_jobs INTEGER NOT NULL
        );
        CREATE TABLE worker_capacity_reservation (
            job_id TEXT PRIMARY KEY, host_id TEXT NOT NULL, request_sha256 TEXT NOT NULL,
            token TEXT NOT NULL, memory_bytes INTEGER NOT NULL, expires_at TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('reserved','dispatched','released')),
            FOREIGN KEY(host_id) REFERENCES worker_capacity_budget(host_id)
        );
        CREATE INDEX worker_capacity_host ON worker_capacity_reservation(host_id,state);
        """
    )
    connection.commit()
    connection.close()

    registry = SQLiteDurableJobRegistry(path, clock=lambda: NOW.isoformat())
    lease = registry.reserve_capacity(_budget(), "migrated", "a" * 64, 1)
    assert lease.physical_host_fingerprint
    assert registry.list_capacity()[0].physical_host_fingerprint == lease.physical_host_fingerprint


def test_physical_fence_rejects_identity_change_even_when_idle(tmp_path):
    # Reconcile an old lease, then ensure its durable authority cannot silently
    # be rebound to another physical host with the same operator label.
    old = ["2026-09-05T12:00:00+00:00"]
    clocked = SQLiteDurableJobRegistry(tmp_path / "clocked.db", clock=lambda: old[0])
    clocked.reserve_capacity(_budget(fingerprint="a" * 64), "one", "a" * 64, 1, lease_seconds=1)
    old[0] = "2026-09-05T12:00:02+00:00"
    clocked.reconcile_capacity()
    with pytest.raises(Conflict, match="physical host identity"):
        clocked.reserve_capacity(_budget(fingerprint="b" * 64), "two", "b" * 64, 1)


def test_reconcile_expires_only_unconsumed_leases_and_is_idempotent(tmp_path):
    now = ["2026-09-05T12:00:00+00:00"]
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: now[0])
    budget = _budget()
    pending = registry.reserve_capacity(budget, "pending", "a" * 64, 40, lease_seconds=1)
    dispatched = registry.reserve_capacity(budget, "running", "b" * 64, 40, lease_seconds=1)
    registry.dispatch_capacity(dispatched.job_id, dispatched.token)
    now[0] = "2026-09-05T12:00:02+00:00"

    result = registry.reconcile_capacity(limit=8)
    assert result.expired_job_ids == (pending.job_id,)
    assert result.expired[0].status == "expired"
    assert registry.reconcile_capacity().expired == ()
    with pytest.raises(CapacityExceeded):
        registry.reserve_capacity(budget, "blocked", "c" * 64, 70)
    assert any(item.status == "expired" for item in registry.list_capacity(include_released=True))


def test_expired_reservation_can_be_rebound_only_with_same_request_identity(tmp_path):
    now = ["2026-09-05T12:00:00+00:00"]
    registry = SQLiteDurableJobRegistry(tmp_path / "jobs.db", clock=lambda: now[0])
    budget = _budget(memory=40)
    registry.reserve_capacity(budget, "same", "a" * 64, None, lease_seconds=1)
    now[0] = "2026-09-05T12:00:02+00:00"
    renewed = registry.reserve_capacity(_budget(memory=100), "same", "a" * 64, None)
    assert renewed.memory_bytes == 100
    with pytest.raises(Conflict):
        registry.reserve_capacity(_budget(memory=100), "same", "b" * 64, None)


def test_placement_queue_is_bounded_priority_ordered_and_explainable():
    queue = PlacementQueue(max_depth=2)
    low = _request("low", priority=1)
    high = _request("high", priority=9)
    rejected = _request("rejected", priority=100)

    assert queue.enqueue(low, now=NOW).reason_code == "queued"
    assert queue.enqueue(high, now=NOW + timedelta(seconds=1)).reason_code == "queued"
    admission = queue.enqueue(rejected, now=NOW + timedelta(seconds=2))
    assert not admission.admitted and admission.reason_code == "queue_full"
    explanation = queue.explain(low.request_id)
    assert explanation.reason_code == "queued"
    assert explanation.position == 2
    assert "priority" in explanation.message
    assert [item.request_id for item in queue.claim(2)] == ["high", "low"]
    assert queue.depth == 0


def test_placement_queue_rejects_conflicting_replay_and_bounds_expiry():
    queue = PlacementQueue(max_depth=4)
    request = _request(priority=2)
    queue.enqueue(request, now=NOW)
    assert queue.enqueue(request, now=NOW + timedelta(seconds=1)).reason_code == "already_queued"
    with pytest.raises(Conflict, match="queue identity"):
        queue.enqueue(replace(request, priority=3), now=NOW)
    expired = queue.expire(now=NOW + timedelta(seconds=10), max_wait_seconds=5, limit=1)
    assert expired[0].reason_code == "queue_expired"
    assert queue.explain(request.request_id).reason_code == "not_queued"


def test_scheduler_rejects_known_queue_pressure_and_exposes_explanation():
    scheduler = ComputePlacementScheduler()
    decision = scheduler.place(
        _request(max_queue_depth=1),
        (_snapshot(active_jobs=1),),
        now=NOW,
    )
    assert decision.selected_node_id is None
    explanation = decision.explain()[0]
    assert explanation.reason_code == "queue_full"
    assert explanation.message
    assert explanation.selected is False


def test_indexed_capability_candidates_are_the_registry_selection_port():
    from sonder_runtime.application.compute_fabric.registry import ComputeNodeRegistry

    snapshot = _snapshot()
    registry = ComputeNodeRegistry((snapshot.node,))
    registry.observe(snapshot)
    candidates = registry.capability_candidates(
        required_capabilities=frozenset({ComputeCapability.CPU}), now=NOW
    )
    assert candidates.snapshots[0].node.node_id == "worker"
    assert candidates.scope.candidate_scope == "indexed_structural_candidates"
