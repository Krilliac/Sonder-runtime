from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sonder_runtime.application.compute_fabric.jobs import RemoteJobEnvelope, RemoteJobReceipt
from sonder_runtime.application.compute_fabric.registry import ComputeNodeRegistry
from sonder_runtime.application.compute_fabric.service import ComputeFabricService
from sonder_runtime.domain.common.errors import DependencyUnavailable
from sonder_runtime.domain.compute_fabric import (
    ComputeCapability,
    ComputeNode,
    ComputePlacementScheduler,
    NodeHealth,
    NodeResources,
    NodeSnapshot,
    WorkloadKind,
    WorkloadRequest,
)


NOW = datetime(2026, 8, 31, 20, tzinfo=timezone.utc)


def _node(node_id: str, *, local: bool) -> ComputeNode:
    return ComputeNode(
        node_id=node_id,
        origin=None if local else f"https://{node_id}.example:8443",
        local=local,
        allowed_workloads=frozenset({WorkloadKind.BUILD}),
        configured_capabilities=frozenset({ComputeCapability.CPU, ComputeCapability.CMAKE}),
        workspace_mappings=frozenset({"sonder"}),
    )


def _snapshot(node: ComputeNode, *, age_seconds: int = 0) -> NodeSnapshot:
    return NodeSnapshot(
        node=node,
        observed_at=NOW - timedelta(seconds=age_seconds),
        health=NodeHealth.HEALTHY,
        live_capabilities=node.configured_capabilities,
        advertised_workloads=node.allowed_workloads,
        resources=NodeResources(cpu_count=16, free_ram_bytes=16 << 30),
    )


def _request(*, allow_local_fallback: bool = False) -> WorkloadRequest:
    return WorkloadRequest(
        request_id="controller-job",
        kind=WorkloadKind.BUILD,
        workspace_mapping="sonder",
        allow_remote=True,
        allow_local_fallback=allow_local_fallback,
        idempotent=True,
    )


def _envelope() -> RemoteJobEnvelope:
    return RemoteJobEnvelope.create(
        controller_job_id="controller-job",
        idempotency_key="idem-1",
        workload=WorkloadKind.BUILD,
        catalog_entry_id="cmake-build",
        workspace_mapping="sonder",
        deadline_seconds=300,
        idempotent=True,
    )


def _receipt(worker_id: str, remote_job_id: str = "remote-1") -> RemoteJobReceipt:
    envelope = _envelope()
    return RemoteJobReceipt(
        worker_id=worker_id,
        remote_job_id=remote_job_id,
        controller_job_id=envelope.controller_job_id,
        idempotency_key=envelope.idempotency_key,
        request_sha256=envelope.request_sha256,
        state="running",
    )


class _LocalWorker:
    def __init__(self) -> None:
        self.calls = 0

    def submit(self, _envelope):
        self.calls += 1
        return _receipt("local", "local-1")


class _Transport:
    def __init__(self, *, ambiguous: bool = False) -> None:
        self.ambiguous = ambiguous
        self.submit_calls = 0
        self.lookup_calls = 0

    def submit(self, node, _envelope):
        self.submit_calls += 1
        if self.ambiguous:
            raise DependencyUnavailable("timeout after request body was sent")
        return _receipt(node.node_id)

    def by_idempotency(self, node, _key):
        self.lookup_calls += 1
        return _receipt(node.node_id, "already-running")


def _service(*, remote_age: int = 0, ambiguous: bool = False):
    local = _node("local", local=True)
    remote = _node("linux-node", local=False)
    registry = ComputeNodeRegistry((local, remote), snapshot_ttl=timedelta(seconds=30))
    registry.observe(_snapshot(local))
    registry.observe(_snapshot(remote, age_seconds=remote_age))
    transport = _Transport(ambiguous=ambiguous)
    local_worker = _LocalWorker()
    service = ComputeFabricService(
        registry=registry,
        scheduler=ComputePlacementScheduler(snapshot_ttl=timedelta(seconds=30)),
        transport=transport,
        local_worker=local_worker,
        now=lambda: NOW,
    )
    return service, transport, local_worker


def test_submit_timeout_reconciles_idempotent_request_before_retry() -> None:
    service, transport, _local = _service(ambiguous=True)
    result = service.submit(_request(), _envelope())
    assert result.receipt.remote_job_id == "already-running"
    assert transport.submit_calls == 1
    assert transport.lookup_calls == 1


def test_no_eligible_remote_node_uses_local_only_when_explicitly_allowed() -> None:
    service, _transport, local = _service(remote_age=60)
    result = service.submit(_request(allow_local_fallback=True), _envelope())
    assert result.node_id == "local"
    assert local.calls == 1

    service, _transport, local = _service(remote_age=60)
    with pytest.raises(DependencyUnavailable, match="eligible remote"):
        service.submit(_request(allow_local_fallback=False), _envelope())
    assert local.calls == 0


def test_request_and_envelope_identity_must_match() -> None:
    service, _transport, _local = _service()
    mismatched = WorkloadRequest(request_id="different", kind=WorkloadKind.BUILD)
    with pytest.raises(ValueError, match="controller identity"):
        service.submit(mismatched, _envelope())
