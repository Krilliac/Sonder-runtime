"""Synthetic P6 acceptance checks for larger configured clusters.

These tests exercise pure contracts and in-process adapters with simulated
workers.  They establish deterministic bounds and failure reporting; they do
not claim real-node throughput, network scale, replication, or HA takeover.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

import pytest

from sonder_runtime.application.compute_fabric.coordinator import ComputeRefreshCoordinator
from sonder_runtime.application.compute_fabric.registry import ComputeNodeRegistry
from sonder_runtime.application.operations.graceful_drain import (
    DrainStage,
    GracefulDrainCoordinator,
    GracefulDrainRequest,
)
from sonder_runtime.application.operations.startup_reconciliation import (
    RecordKind,
    StartupObservation,
)
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
    WorkloadKind,
)
from sonder_runtime.domain.scheduler_partition import (
    PartitionDescriptor,
    PartitionRouter,
    PartitionRoutingError,
)


NOW = datetime(2026, 9, 5, 12, tzinfo=timezone.utc)
CLUSTER_SIZES = (16, 64, 256)


def _descriptors(size: int) -> tuple[PartitionDescriptor, ...]:
    return tuple(
        PartitionDescriptor(
            f"partition-{index:04d}",
            f"worker-{index:04d}",
            capacity=1 + index % 4,
        )
        for index in range(size)
    )


@pytest.mark.parametrize("size", CLUSTER_SIZES)
def test_simulated_cluster_enrollment_pagination_and_stable_session_routing(size):
    """Every simulated worker enrolls once and routes are order-independent."""
    descriptors = _descriptors(size)
    router = PartitionRouter(protocol_version=2, max_partitions=size)
    for descriptor in descriptors:
        router.upsert(descriptor)

    first_page = router.page(limit=1)
    revision = first_page.revision
    ids: list[str] = []
    cursor = None
    while True:
        page = router.page(after=cursor, limit=31, snapshot_revision=revision)
        ids.extend(item.partition_id for item in page.items)
        assert len(page.items) <= 31
        if page.complete:
            break
        cursor = page.next_cursor

    assert ids == [descriptor.partition_id for descriptor in descriptors]
    assert len(router.partitions) == size

    keys = tuple(f"session-{index:04d}" for index in range(512))
    assignments = {key: router.route(key).partition_id for key in keys}
    reversed_router = PartitionRouter(reversed(descriptors), protocol_version=2, max_partitions=size)
    assert {key: reversed_router.route(key).partition_id for key in keys} == assignments
    assert all(router.route(key).partition_id == assignments[key] for key in keys)


@pytest.mark.parametrize("size", CLUSTER_SIZES)
def test_simulated_partition_protocol_mismatch_and_snapshot_fence(size):
    descriptors = _descriptors(size)
    router = PartitionRouter(descriptors, protocol_version=2, max_partitions=size)
    assert router.negotiate(2).accepted
    rejected = router.negotiate(1)
    assert rejected.accepted is False
    assert rejected.reason == "protocol_version_mismatch"

    page = router.page(limit=32)
    router.upsert(
        PartitionDescriptor(
            descriptors[-1].partition_id,
            descriptors[-1].owner_id,
            capacity=descriptors[-1].capacity,
            revision=2,
            status="draining",
        )
    )
    with pytest.raises(PartitionRoutingError, match="revision changed"):
        router.page(after=page.next_cursor, limit=32, snapshot_revision=page.revision)
    assert router.route("session-after-drain").status == "active"


def _compute_cluster(size: int):
    nodes = tuple(
        ComputeNode(
            node_id=f"worker-{index:04d}",
            origin=f"https://worker-{index:04d}.example:8443",
            local=False,
            allowed_workloads=frozenset({WorkloadKind.BUILD}),
            configured_capabilities=frozenset({ComputeCapability.CPU}),
        )
        for index in range(size)
    )
    snapshots = {
        node.node_id: NodeSnapshot(
            node=node,
            observed_at=NOW,
            received_at=NOW,
            health=NodeHealth.HEALTHY,
            live_capabilities=frozenset({ComputeCapability.CPU}),
            advertised_workloads=frozenset({WorkloadKind.BUILD}),
            resources=NodeResources(cpu_count=4, free_ram_bytes=8 << 30),
        )
        for node in nodes
    }
    return ComputeNodeRegistry(nodes), snapshots


@pytest.mark.parametrize("size", CLUSTER_SIZES)
def test_simulated_refresh_never_submits_more_than_eight_probes(size):
    registry, snapshots = _compute_cluster(size)
    release = Event()
    started = Event()
    lock = Lock()
    calls: list[str] = []
    active = 0
    peak = 0

    class Source:
        def snapshot(self, node, *, now):
            nonlocal active, peak
            with lock:
                calls.append(node.node_id)
                active += 1
                peak = max(peak, active)
                if active == min(8, size):
                    started.set()
            release.wait(5)
            with lock:
                active -= 1
            return snapshots[node.node_id]

    coordinator = ComputeRefreshCoordinator(
        registry,
        Source(),
        now=lambda: NOW,
        refresh_after=timedelta(seconds=10),
    )
    failures: list[BaseException] = []

    def refresh():
        try:
            coordinator.refresh(force=True)
        except BaseException as error:  # surfaced below, never swallowed
            failures.append(error)

    thread = Thread(target=refresh)
    thread.start()
    try:
        assert started.wait(3)
        assert coordinator.state()["submitted"] <= 8
    finally:
        release.set()
        thread.join(15)
        coordinator.close()

    assert not thread.is_alive()
    assert failures == []
    assert peak <= 8
    assert len(calls) == size
    assert len(registry.list_snapshots(now=NOW)) == size


@dataclass
class _DrainAdmission:
    calls: list[str]

    def stop_admission(self, reason: str) -> bool:
        self.calls.append(reason)
        return True


@dataclass
class _DrainDeadline:
    calls: list[object]

    def announce_deadline(self, notice) -> bool:
        self.calls.append(notice)
        return True


@dataclass
class _DrainDescendants:
    cancellations: list[str]
    settlements: list[float]

    def cancel_descendants(self, reason: str) -> bool:
        self.cancellations.append(reason)
        return True

    def settle_descendants(self, deadline_monotonic: float) -> bool:
        self.settlements.append(deadline_monotonic)
        return True


class _DrainProcessTree:
    def __init__(self):
        self.requests = []

    def cleanup(self, request):
        from sonder_runtime.application.jobs.durable_registry import ProcessTreeCleanupReceipt

        self.requests.append(request)
        return ProcessTreeCleanupReceipt(
            request.job_id,
            True,
            1,
            1,
            True,
            "terminated",
        )


@pytest.mark.parametrize("size", CLUSTER_SIZES)
def test_simulated_cluster_drain_is_bounded_and_never_calls_truncated_clean(size):
    observations = tuple(
        StartupObservation(
            RecordKind.JOB,
            f"job-{index:04d}",
            "running",
            owner_alive=False,
            process_id=1000 + index,
            process_group_id=2000 + index,
        )
        for index in range(size)
    )
    admission = _DrainAdmission([])
    deadline = _DrainDeadline([])
    descendants = _DrainDescendants([], [])
    process_tree = _DrainProcessTree()
    coordinator = GracefulDrainCoordinator(
        admission=admission,
        descendants=descendants,
        deadline_communicator=deadline,
        flush=lambda remaining: remaining >= 0,
        cleanup=lambda remaining: remaining >= 0,
        process_tree=process_tree,
        clock=lambda: 0.0,
    )
    result = coordinator.drain(
        GracefulDrainRequest(
            reason=f"synthetic-{size}",
            deadline_seconds=5,
            max_records=64,
            max_process_descendants=32,
        ),
        observations=observations,
    )

    assert len(result.plan.results) == min(size, 64)
    assert len(process_tree.requests) == min(size, 64)
    assert all(request.max_descendants == 32 for request in process_tree.requests)
    if size > 64:
        assert result.plan.truncated is True
        assert result.stage is DrainStage.INCOMPLETE
        assert result.clean is False
        assert any("drain plan truncated" in error for error in result.errors)
    else:
        assert result.plan.truncated is False
        assert result.clean is True
