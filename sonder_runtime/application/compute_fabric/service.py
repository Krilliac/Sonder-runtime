"""Whole-job placement and ambiguity-safe compute dispatch."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from threading import RLock
from typing import Callable

from .jobs import (
    ComputeJobWorker,
    RemoteJobEnvelope,
    RemoteJobReceipt,
    validate_remote_job_receipt,
)
from .registry import ComputeNodeRegistry
from ..ports.compute_fabric import ComputeRemoteJobTransport
from ...domain.common.errors import DependencyUnavailable, NotFound
from ...domain.compute_fabric import (
    ComputePlacementScheduler,
    PlacementDecision,
    WorkloadRequest,
)
from ...domain.compute_profiles import profile_for


@dataclass(frozen=True, slots=True)
class ComputeSubmission:
    node_id: str
    placement: PlacementDecision
    receipt: RemoteJobReceipt


@dataclass(frozen=True, slots=True)
class _PlacementRecord:
    node_id: str
    placement: PlacementDecision
    envelope: RemoteJobEnvelope
    remote_job_id: str | None = None


class ComputeFabricService:
    """Place bounded catalog jobs on one live eligible node."""

    def __init__(
        self,
        *,
        registry: ComputeNodeRegistry,
        scheduler: ComputePlacementScheduler,
        transport: ComputeRemoteJobTransport,
        local_worker: ComputeJobWorker,
        now: Callable[[], datetime],
        refresh: Callable[[], None] | None = None,
    ) -> None:
        self._registry = registry
        self._scheduler = scheduler
        self._transport = transport
        self._local_worker = local_worker
        self._now = now
        self._refresh = refresh
        self._placements: dict[str, _PlacementRecord] = {}
        self._lock = RLock()

    @staticmethod
    def _profiled(request: WorkloadRequest) -> WorkloadRequest:
        profile = profile_for(request.kind)
        if profile.requires_workspace and request.workspace_mapping is None:
            raise ValueError(f"{request.kind.value} workloads require a workspace mapping")
        return replace(
            request,
            required_capabilities=(
                request.required_capabilities | profile.all_capabilities
            ),
            any_capabilities=(
                request.any_capabilities | profile.any_capabilities
            ),
        )

    def _place(self, request: WorkloadRequest) -> PlacementDecision:
        if self._refresh is not None:
            self._refresh()
        now = self._now()
        snapshots = self._registry.list_snapshots(now=now)
        if request.local_only or not request.allow_remote:
            candidates = tuple(item for item in snapshots if item.node.local)
            return self._scheduler.place(request, candidates, now=now)

        remote = tuple(item for item in snapshots if not item.node.local)
        decision = self._scheduler.place(request, remote, now=now)
        if decision.selected_node_id is not None or not request.allow_local_fallback:
            return decision
        local = tuple(item for item in snapshots if item.node.local)
        return self._scheduler.place(request, local, now=now)

    def submit(
        self,
        request: WorkloadRequest,
        envelope: RemoteJobEnvelope,
    ) -> ComputeSubmission:
        if request.request_id != envelope.controller_job_id:
            raise ValueError("workload and envelope controller identity must match")
        if request.kind is not envelope.workload:
            raise ValueError("workload and envelope kinds must match")
        if request.workspace_mapping != envelope.workspace_mapping:
            raise ValueError("workload and envelope workspace mappings must match")
        if request.idempotent != envelope.idempotent:
            raise ValueError("workload and envelope idempotency must match")
        profiled = self._profiled(request)
        placement = self._place(profiled)
        node_id = placement.selected_node_id
        if node_id is None:
            scope = "eligible node" if request.local_only else "eligible remote node"
            raise DependencyUnavailable(f"no {scope} is available for this workload")
        node = self._registry.get_node(node_id)
        # Retain the digest-bound placement before any call whose outcome can
        # become ambiguous to the controller.
        with self._lock:
            self._placements[request.request_id] = _PlacementRecord(
                node_id, placement, envelope,
            )
        if node.local:
            receipt = self._local_worker.submit(envelope)
        else:
            try:
                receipt = self._transport.submit(node, envelope)
            except DependencyUnavailable:
                if not envelope.idempotent:
                    raise
                receipt = self._transport.by_idempotency(
                    node, envelope.idempotency_key
                )
                if receipt is None:
                    raise
        validate_remote_job_receipt(
            receipt,
            worker_id=node_id,
            controller_job_id=envelope.controller_job_id,
            idempotency_key=envelope.idempotency_key,
            request_sha256=envelope.request_sha256,
        )
        with self._lock:
            self._placements[request.request_id] = _PlacementRecord(
                node_id, placement, envelope, receipt.remote_job_id,
            )
        return ComputeSubmission(node_id, placement, receipt)

    def status(self, controller_job_id: str) -> ComputeSubmission:
        with self._lock:
            placed = self._placements.get(controller_job_id)
        if placed is None or placed.remote_job_id is None:
            raise NotFound("compute placement was not found")
        node_id = placed.node_id
        placement = placed.placement
        remote_job_id = placed.remote_job_id
        node = self._registry.get_node(node_id)
        receipt = (
            self._local_worker.status(remote_job_id)
            if node.local
            else self._transport.status(node, remote_job_id)
        )
        validate_remote_job_receipt(
            receipt,
            worker_id=node_id,
            controller_job_id=placed.envelope.controller_job_id,
            idempotency_key=placed.envelope.idempotency_key,
            request_sha256=placed.envelope.request_sha256,
            remote_job_id=remote_job_id,
        )
        return ComputeSubmission(node_id, placement, receipt)

    def cancel(self, controller_job_id: str, *, reason: str) -> ComputeSubmission:
        with self._lock:
            placed = self._placements.get(controller_job_id)
        if placed is None or placed.remote_job_id is None:
            raise NotFound("compute placement was not found")
        node_id = placed.node_id
        placement = placed.placement
        remote_job_id = placed.remote_job_id
        node = self._registry.get_node(node_id)
        receipt = (
            self._local_worker.cancel(remote_job_id, reason=reason)
            if node.local
            else self._transport.cancel(node, remote_job_id, reason=reason)
        )
        validate_remote_job_receipt(
            receipt,
            worker_id=node_id,
            controller_job_id=placed.envelope.controller_job_id,
            idempotency_key=placed.envelope.idempotency_key,
            request_sha256=placed.envelope.request_sha256,
            remote_job_id=remote_job_id,
        )
        return ComputeSubmission(node_id, placement, receipt)


__all__ = ["ComputeFabricService", "ComputeSubmission"]
