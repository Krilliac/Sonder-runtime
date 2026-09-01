"""Whole-job placement and ambiguity-safe compute dispatch."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
from threading import RLock
from typing import Callable

from .jobs import (
    ComputeJobWorker,
    RemoteArtifactPayload,
    RemoteJobEnvelope,
    RemoteJobReceipt,
    validate_remote_job_receipt,
)
from .registry import ComputeNodeRegistry
from ..ports.compute_fabric import ComputeRemoteJobTransport
from ...domain.common.errors import Conflict, DependencyUnavailable, NotFound
from ...domain.compute_fabric import (
    CandidateDecision,
    ComputePlacementScheduler,
    PlacementDecision,
    WorkloadRequest,
    NodeHealth,
)
from ...domain.compute_profiles import profile_for
from ..ports.jobs import JobIdentity, JobStatus


@dataclass(frozen=True, slots=True)
class ComputeSubmission:
    node_id: str
    placement: PlacementDecision
    receipt: RemoteJobReceipt


@dataclass(frozen=True, slots=True)
class _PlacementRecord:
    node_id: str
    placement: PlacementDecision
    controller_job_id: str
    idempotency_key: str
    request_sha256: str
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
        placement_registry=None,
        metrics=None,
    ) -> None:
        self._registry = registry
        self._scheduler = scheduler
        self._transport = transport
        self._local_worker = local_worker
        self._now = now
        self._refresh = refresh
        self._placement_registry = placement_registry
        self._metrics = metrics
        self._placements: dict[str, _PlacementRecord] = {}
        self._lock = RLock()
        self._rehydrate_placements()

    @staticmethod
    def _placement_payload(record: _PlacementRecord) -> dict:
        return {
            "node_id": record.node_id,
            "remote_job_id": record.remote_job_id,
            "request_digest": record.placement.request_digest,
            "candidates": [
                {
                    "node_id": candidate.node_id,
                    "eligible": candidate.eligible,
                    "reason_code": candidate.reason_code,
                    "score": candidate.score,
                }
                for candidate in record.placement.candidates
            ],
            "ranked_node_ids": list(record.placement.ranked_node_ids),
            "snapshot_digests": [list(item) for item in record.placement.snapshot_digests],
        }

    def _rehydrate_placements(self) -> None:
        store = self._placement_registry
        list_records = getattr(store, "list", None)
        view_record = getattr(store, "view", None)
        if not callable(list_records) or not callable(view_record):
            return
        recovered: dict[str, _PlacementRecord] = {}
        iterator = getattr(store, "iter_kind", None)
        jobs = (
            iterator("compute-placement", include_terminal=True)
            if callable(iterator)
            else list_records(include_terminal=True, limit=1024)
        )
        for job in jobs:
            if job.identity.kind != "compute-placement":
                continue
            metadata = getattr(view_record(job.identity.job_id), "metadata", None) or {}
            payload = (
                job.result
                if isinstance(job.result, dict)
                else metadata.get("placement_payload")
            )
            if not isinstance(payload, dict):
                continue
            try:
                placement = PlacementDecision(
                    request_digest=str(payload["request_digest"]),
                    selected_node_id=str(payload["node_id"]),
                    candidates=tuple(
                        CandidateDecision(
                            node_id=str(item["node_id"]),
                            eligible=bool(item["eligible"]),
                            reason_code=str(item["reason_code"]),
                            score=item.get("score"),
                        )
                        for item in payload.get("candidates", ())
                    ),
                    ranked_node_ids=tuple(str(item) for item in payload.get("ranked_node_ids", ())),
                    snapshot_digests=tuple(
                        (str(item[0]), str(item[1]))
                        for item in payload.get("snapshot_digests", ())
                    ),
                )
                controller_job_id = str(metadata["controller_job_id"])
                recovered[controller_job_id] = _PlacementRecord(
                    node_id=str(payload["node_id"]),
                    placement=placement,
                    controller_job_id=controller_job_id,
                    idempotency_key=job.identity.idempotency_key,
                    request_sha256=str(metadata["request_sha256"]),
                    remote_job_id=(
                        str(payload["remote_job_id"])
                        if payload.get("remote_job_id") is not None
                        else None
                    ),
                )
            except (KeyError, TypeError, ValueError):
                continue
        with self._lock:
            self._placements.update(recovered)

    def _persist_placement(self, record: _PlacementRecord, *, create: bool = False) -> None:
        store = self._placement_registry
        if store is None:
            return
        storage_job_id = "cp-" + hashlib.sha256(
            record.controller_job_id.encode("utf-8")
        ).hexdigest()[:24]
        if create:
            store.start(
                JobIdentity(
                    storage_job_id,
                    "compute-placement",
                    record.controller_job_id,
                    record.idempotency_key,
                ),
                metadata={
                    "controller_job_id": record.controller_job_id,
                    "request_sha256": record.request_sha256,
                    "placement_payload": self._placement_payload(record),
                },
            )
        store.transition(
            storage_job_id,
            JobStatus.RUNNING,
            result=self._placement_payload(record),
        )

    def _record_receipt(
        self, record: _PlacementRecord, receipt: RemoteJobReceipt,
    ) -> _PlacementRecord:
        updated = replace(record, remote_job_id=receipt.remote_job_id)
        with self._lock:
            self._placements[record.controller_job_id] = updated
        self._persist_placement(updated)
        return updated

    def _lookup_ambiguous(self, record: _PlacementRecord) -> RemoteJobReceipt:
        node = self._registry.get_node(record.node_id)
        receipt = (
            self._local_worker.by_idempotency(record.idempotency_key)
            if node.local
            else self._transport.by_idempotency(node, record.idempotency_key)
        )
        if receipt is None:
            raise DependencyUnavailable("compute placement outcome remains ambiguous")
        validate_remote_job_receipt(
            receipt,
            worker_id=record.node_id,
            controller_job_id=record.controller_job_id,
            idempotency_key=record.idempotency_key,
            request_sha256=record.request_sha256,
        )
        self._record_receipt(record, receipt)
        return receipt

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
            background_preferred=(
                request.background_preferred or profile.background_preferred
            ),
        )

    def _place(self, request: WorkloadRequest) -> PlacementDecision:
        if self._refresh is not None:
            self._refresh()
        now = self._now()
        snapshots = self._registry.list_snapshots(now=now)
        self._observe_inventory(snapshots, now=now)
        if request.local_only or not request.allow_remote:
            candidates = tuple(item for item in snapshots if item.node.local)
            decision = self._scheduler.place(request, candidates, now=now)
            if decision.selected_node_id is None:
                self._observe_rejection(decision)
            return decision

        remote = tuple(item for item in snapshots if not item.node.local)
        decision = self._scheduler.place(request, remote, now=now)
        if decision.selected_node_id is not None or not request.allow_local_fallback:
            if decision.selected_node_id is None:
                self._observe_rejection(decision)
            return decision
        local = tuple(item for item in snapshots if item.node.local)
        local_decision = self._scheduler.place(request, local, now=now)
        if local_decision.selected_node_id is None:
            self._observe_rejection(local_decision)
        return local_decision

    def _observe_inventory(self, snapshots, *, now: datetime) -> None:
        observe = getattr(self._metrics, "set_compute_inventory", None)
        if not callable(observe):
            return
        stale_ids = {
            snapshot.node.node_id
            for snapshot in snapshots
            if self._registry.is_stale(snapshot.node.node_id, now=now)
        }
        observe(
            configured=len(self._registry.configured_nodes()),
            live=sum(1 for item in snapshots if item.node.node_id not in stale_ids),
            healthy=sum(
                1 for item in snapshots
                if item.node.node_id not in stale_ids and item.health is NodeHealth.HEALTHY
            ),
            unhealthy=sum(
                1 for item in snapshots
                if item.node.node_id not in stale_ids and item.health is NodeHealth.UNHEALTHY
            ),
            stale=len(stale_ids),
            active_jobs=sum(item.active_jobs for item in snapshots),
        )

    def _observe_rejection(self, decision: PlacementDecision) -> None:
        observe = getattr(self._metrics, "observe_compute_placement_rejection", None)
        if not callable(observe):
            return
        reason = decision.candidates[0].reason_code if decision.candidates else "no_candidates"
        observe(reason=reason)

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
        profile = profile_for(request.kind)
        if profile.requires_digest_bound_input and not envelope.input_artifacts:
            raise ValueError(
                f"{request.kind.value} workloads require digest-bound input artifacts"
            )
        with self._lock:
            existing = self._placements.get(request.request_id)
        if existing is not None:
            if (
                existing.idempotency_key != envelope.idempotency_key
                or existing.request_sha256 != envelope.request_sha256
            ):
                raise Conflict(
                    "controller identity is already bound to another compute request"
                )
            return self.status(request.request_id)
        profiled = self._profiled(request)
        placement = self._place(profiled)
        node_id = placement.selected_node_id
        if node_id is None:
            scope = "eligible node" if request.local_only else "eligible remote node"
            raise DependencyUnavailable(f"no {scope} is available for this workload")
        node = self._registry.get_node(node_id)
        # Retain the digest-bound placement before any call whose outcome can
        # become ambiguous to the controller.
        placed = _PlacementRecord(
            node_id,
            placement,
            envelope.controller_job_id,
            envelope.idempotency_key,
            envelope.request_sha256,
        )
        with self._lock:
            self._placements[request.request_id] = placed
        self._persist_placement(placed, create=True)
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
        self._record_receipt(placed, receipt)
        observe_placement = getattr(self._metrics, "observe_compute_placement", None)
        if callable(observe_placement):
            observe_placement(route="local" if node.local else "remote")
        return ComputeSubmission(node_id, placement, receipt)

    def status(self, controller_job_id: str) -> ComputeSubmission:
        with self._lock:
            placed = self._placements.get(controller_job_id)
        if placed is None:
            raise NotFound("compute placement was not found")
        if placed.remote_job_id is None:
            receipt = self._lookup_ambiguous(placed)
            return ComputeSubmission(placed.node_id, placed.placement, receipt)
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
            controller_job_id=placed.controller_job_id,
            idempotency_key=placed.idempotency_key,
            request_sha256=placed.request_sha256,
            remote_job_id=remote_job_id,
        )
        return ComputeSubmission(node_id, placement, receipt)

    def cancel(self, controller_job_id: str, *, reason: str) -> ComputeSubmission:
        with self._lock:
            placed = self._placements.get(controller_job_id)
        if placed is None:
            raise NotFound("compute placement was not found")
        if placed.remote_job_id is None:
            self._lookup_ambiguous(placed)
            with self._lock:
                placed = self._placements[controller_job_id]
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
            controller_job_id=placed.controller_job_id,
            idempotency_key=placed.idempotency_key,
            request_sha256=placed.request_sha256,
            remote_job_id=remote_job_id,
        )
        return ComputeSubmission(node_id, placement, receipt)

    def fetch_artifact(
        self,
        controller_job_id: str,
        name: str,
        *,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> RemoteArtifactPayload:
        submission = self.status(controller_job_id)
        expected = next(
            (artifact for artifact in submission.receipt.artifacts if artifact.name == name),
            None,
        )
        if expected is None:
            raise NotFound("compute artifact was not found")
        if expected.size_bytes > max_bytes:
            raise DependencyUnavailable("compute artifact exceeds the requested transfer bound")
        node = self._registry.get_node(submission.node_id)
        payload = (
            self._local_worker.read_artifact(
                submission.receipt.remote_job_id, name, max_bytes=max_bytes,
            )
            if node.local
            else self._transport.fetch_artifact(
                node, submission.receipt.remote_job_id, expected,
            )
        )
        if payload.receipt != expected:
            raise DependencyUnavailable("compute artifact payload receipt mismatch")
        return payload


__all__ = ["ComputeFabricService", "ComputeSubmission"]
