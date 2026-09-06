"""Bounded admission of an immutable model deployment onto worker capacity.

This service composes two existing contracts:

* :class:`~sonder_runtime.domain.model_deployment.ModelDeployment` supplies the
  immutable model/backend/topology identity; and
* :class:`WorkerCapacity` owns the worker's durable resource reservations.

The service does not place ranks, start a model backend, transfer weights,
elect a coordinator, or implement a consensus protocol.  The caller supplies
one already selected worker budget for every manifest rank.  Admission binds
that exact plan to a stable digest, reserves one bounded capacity lease per
rank, and dispatches those leases before returning a receipt.

The in-memory admission index is deliberately process-local.  A future
durable adapter must rebuild it from its own authoritative deployment journal;
this slice never treats a remembered receipt as authority after restart.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
import hashlib
import json
import re
from threading import RLock
from collections.abc import Iterable, Mapping

from ...domain.common.errors import (
    CapacityExceeded,
    Conflict,
    DependencyUnavailable,
    NotFound,
    SonderError,
)
from ...domain.model_deployment import ModelDeployment, ModelRank
from ...domain.worker_capacity import (
    CapacityReconciliation,
    CapacityReservation,
    WorkerBudget,
    bounded_positive,
)
from .capacity import WorkerCapacity


_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_MAX_ACTIVE_ADMISSIONS = 256
_MAX_RECONCILIATION_ROWS = 1024


def _identity(value: object, field: str) -> str:
    if not isinstance(value, str) or _IDENTITY.fullmatch(value) is None:
        raise ValueError(f"{field} must be a bounded stable identity")
    return value


def _digest(value: object, field: str) -> str:
    if not isinstance(value, str) or _DIGEST.fullmatch(value) is None:
        raise ValueError(f"{field} must be a canonical SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class DeploymentResourceRequest:
    """One caller-selected worker budget for one manifest rank.

    ``memory_bytes`` is an explicit reservation demand.  It is not inferred
    from model parameters or a live RAM probe, and the worker remains the
    authority that accepts or rejects the budget.
    """

    rank: ModelRank
    budget: WorkerBudget
    memory_bytes: int

    def __post_init__(self) -> None:
        if not isinstance(self.rank, ModelRank):
            raise TypeError("deployment rank must be a ModelRank")
        if not isinstance(self.budget, WorkerBudget):
            raise TypeError("deployment budget must be a WorkerBudget")
        bounded_positive(self.memory_bytes, "deployment memory_bytes")


@dataclass(frozen=True, slots=True)
class DeploymentReservation:
    """Redacted public reservation evidence for one deployment rank."""

    rank: int
    host_id: str
    worker_id: str
    device_id: str
    job_id: str
    memory_bytes: int
    state: str = "dispatched"

    def __post_init__(self) -> None:
        if type(self.rank) is not int or not 0 <= self.rank <= 255:
            raise ValueError("deployment reservation rank must be within 0..255")
        for field in ("host_id", "worker_id", "device_id", "job_id"):
            _identity(getattr(self, field), field)
        bounded_positive(self.memory_bytes, "deployment reservation memory_bytes")
        if self.state != "dispatched":
            raise ValueError("deployment reservation must be dispatched")


@dataclass(frozen=True, slots=True)
class DeploymentAdmissionReceipt:
    """Immutable, redacted evidence returned after every rank is admitted."""

    cluster_id: str
    deployment_digest: str
    reservation_group: str
    plan_digest: str
    reservations: tuple[DeploymentReservation, ...]
    state: str = "admitted"

    def __post_init__(self) -> None:
        _identity(self.cluster_id, "cluster_id")
        _digest(self.deployment_digest, "deployment_digest")
        _identity(self.reservation_group, "reservation_group")
        _digest(self.plan_digest, "plan_digest")
        if type(self.reservations) is not tuple or not self.reservations:
            raise ValueError("deployment reservations must be a non-empty tuple")
        if any(not isinstance(item, DeploymentReservation) for item in self.reservations):
            raise TypeError("deployment reservations must contain typed evidence")
        ranks = tuple(item.rank for item in self.reservations)
        if ranks != tuple(range(len(ranks))):
            raise ValueError("deployment reservations must cover contiguous ranks")
        if self.state != "admitted":
            raise ValueError("deployment admission receipt state is invalid")


@dataclass(frozen=True, slots=True)
class DeploymentReconciliation:
    """Bounded worker reconciliation projected onto active admissions."""

    observed_at: str
    expired_job_ids: tuple[str, ...]
    affected_plan_digests: tuple[str, ...]
    inspected: int

    def __post_init__(self) -> None:
        if not isinstance(self.observed_at, str) or not self.observed_at:
            raise ValueError("reconciliation observed_at is required")
        if type(self.expired_job_ids) is not tuple:
            raise TypeError("expired_job_ids must be an immutable tuple")
        if type(self.affected_plan_digests) is not tuple:
            raise TypeError("affected_plan_digests must be an immutable tuple")
        for job_id in self.expired_job_ids:
            _identity(job_id, "expired job id")
        for digest in self.affected_plan_digests:
            _digest(digest, "affected plan digest")
        if type(self.inspected) is not int or not 0 <= self.inspected <= _MAX_RECONCILIATION_ROWS:
            raise ValueError("reconciliation inspected count is out of bounds")


@dataclass(frozen=True, slots=True)
class _ActiveAdmission:
    receipt: DeploymentAdmissionReceipt
    leases: tuple["_LeaseBinding", ...]


@dataclass(frozen=True, slots=True)
class _LeaseBinding:
    capacity: WorkerCapacity
    lease: CapacityReservation


class DeploymentAdmissionService:
    """Reserve and release the resources named by an immutable deployment.

    The service accepts a structural ``WorkerCapacity`` port and keeps only a
    bounded process-local index of live receipts.  Worker reservation state is
    still durable in the configured worker adapter; the local index is merely
    the authority for which caller receipt may release these leases during the
    current process lifetime.
    """

    def __init__(
        self,
        capacity: WorkerCapacity | Mapping[str, WorkerCapacity],
        *,
        reservation_seconds: int = 30,
        max_active_admissions: int = _MAX_ACTIVE_ADMISSIONS,
    ) -> None:
        required = (
            "reserve_capacity",
            "dispatch_capacity",
            "release_capacity",
            "reconcile_capacity",
        )
        if isinstance(capacity, Mapping):
            if not capacity or len(capacity) > _MAX_ACTIVE_ADMISSIONS:
                raise ValueError(
                    f"capacity map must contain 1..{_MAX_ACTIVE_ADMISSIONS} hosts"
                )
            capacity_map: dict[str, WorkerCapacity] = {}
            for host_id, worker_capacity in capacity.items():
                _identity(host_id, "capacity host_id")
                if any(not callable(getattr(worker_capacity, name, None)) for name in required):
                    raise TypeError("each mapped capacity must implement the WorkerCapacity port")
                capacity_map[host_id] = worker_capacity
            self._capacities = capacity_map
            self._default_capacity = None
        else:
            if any(not callable(getattr(capacity, name, None)) for name in required):
                raise TypeError("capacity must implement the WorkerCapacity port")
            self._capacities = {}
            self._default_capacity = capacity
        bounded_positive(reservation_seconds, "reservation_seconds", 300)
        if type(max_active_admissions) is not int or not 1 <= max_active_admissions <= _MAX_ACTIVE_ADMISSIONS:
            raise ValueError(
                f"max_active_admissions must be within 1..{_MAX_ACTIVE_ADMISSIONS}"
            )
        self._reservation_seconds = reservation_seconds
        self._max_active_admissions = max_active_admissions
        self._active: dict[tuple[str, str], _ActiveAdmission] = {}
        self._lock = RLock()

    def _capacity_for(self, host_id: str) -> WorkerCapacity:
        if self._default_capacity is not None:
            return self._default_capacity
        try:
            return self._capacities[host_id]
        except KeyError:
            raise DependencyUnavailable(
                f"worker capacity is not configured for deployment host {host_id!r}"
            ) from None

    def _capacity_targets(self) -> tuple[WorkerCapacity, ...]:
        candidates = (
            (self._default_capacity,)
            if self._default_capacity is not None
            else tuple(self._capacities[host_id] for host_id in sorted(self._capacities))
        )
        seen: set[int] = set()
        unique: list[WorkerCapacity] = []
        for capacity in candidates:
            marker = id(capacity)
            if marker not in seen:
                seen.add(marker)
                unique.append(capacity)
        return tuple(unique)

    @staticmethod
    def _plan_digest(
        deployment: ModelDeployment,
        resources: tuple[DeploymentResourceRequest, ...],
    ) -> str:
        payload = {
            "schema": "sonder.model-deployment-admission.v1",
            "deployment_digest": deployment.digest,
            "cluster_id": deployment.cluster_id,
            "deployment_id": deployment.deployment_id,
            "revision": deployment.revision,
            "reservation_group": deployment.reservation_group,
            "resources": [
                {
                    "rank": request.rank.rank,
                    "host_id": request.rank.host_id,
                    "worker_id": request.rank.worker_id,
                    "device_id": request.rank.device_id,
                    "budget_host_id": request.budget.host_id,
                    "budget_memory_bytes": request.budget.memory_bytes,
                    "budget_max_jobs": request.budget.max_jobs,
                    "budget_physical_host_fingerprint": request.budget.physical_host_fingerprint,
                    "memory_bytes": request.memory_bytes,
                }
                for request in resources
            ],
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _job_id(deployment: ModelDeployment, plan_digest: str, rank: int) -> str:
        material = "\0".join(
            (deployment.cluster_id, deployment.reservation_group, deployment.digest, plan_digest, str(rank))
        ).encode("utf-8")
        return f"md-{hashlib.sha256(material).hexdigest()[:48]}-{rank}"

    @staticmethod
    def _request_digest(plan_digest: str, request: DeploymentResourceRequest) -> str:
        material = "\0".join(
            (
                plan_digest,
                str(request.rank.rank),
                request.rank.host_id,
                request.rank.worker_id,
                request.rank.device_id,
                str(request.memory_bytes),
            )
        ).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    @staticmethod
    def _validate_resources(
        deployment: ModelDeployment,
        resources: Iterable[DeploymentResourceRequest],
    ) -> tuple[DeploymentResourceRequest, ...]:
        if type(resources) is not tuple:
            raise TypeError("deployment resources must be an immutable tuple")
        if len(resources) != len(deployment.ranks):
            raise ValueError("deployment requires exactly one resource request per rank")
        for expected_rank, request in zip(deployment.ranks, resources):
            if not isinstance(request, DeploymentResourceRequest):
                raise TypeError("deployment resources must contain typed requests")
            if request.rank != expected_rank:
                raise ValueError("deployment resources must preserve manifest rank order")
            if request.budget.host_id != expected_rank.host_id:
                raise ValueError("deployment resource budget host must match rank host")
            if request.budget.memory_bytes == 0 or request.memory_bytes > request.budget.memory_bytes:
                raise CapacityExceeded("deployment resource demand exceeds worker budget")
        return resources

    def admit(
        self,
        deployment: ModelDeployment,
        resources: tuple[DeploymentResourceRequest, ...],
    ) -> DeploymentAdmissionReceipt:
        """Admit every rank or return no usable deployment receipt.

        Each successful capacity reservation is dispatched before the receipt
        is published.  If a later reservation or dispatch fails, cleanup is
        attempted for every job touched by this call.  The worker's existing
        contract retains an undispatched lease until expiry when it cannot
        prove immediate cleanup.
        """

        if not isinstance(deployment, ModelDeployment):
            raise TypeError("deployment must be a ModelDeployment")
        resources = self._validate_resources(deployment, resources)
        # Resolve every target before the first reservation.  A missing node
        # capacity entry must never produce a partially admitted deployment.
        targets = tuple(self._capacity_for(request.rank.host_id) for request in resources)
        plan_digest = self._plan_digest(deployment, resources)
        key = (deployment.cluster_id, deployment.reservation_group)
        with self._lock:
            prior = self._active.get(key)
            if prior is not None:
                if (
                    prior.receipt.deployment_digest == deployment.digest
                    and prior.receipt.plan_digest == plan_digest
                ):
                    return prior.receipt
                raise Conflict(
                    "deployment reservation group is already bound to another admission"
                )
            if len(self._active) >= self._max_active_admissions:
                raise CapacityExceeded("deployment admission capacity is full")

            leases: list[_LeaseBinding] = []
            attempted: list[tuple[WorkerCapacity, str]] = []
            try:
                for request, capacity in zip(resources, targets):
                    rank = request.rank.rank
                    job_id = self._job_id(deployment, plan_digest, rank)
                    attempted.append((capacity, job_id))
                    lease = capacity.reserve_capacity(
                        request.budget,
                        job_id,
                        self._request_digest(plan_digest, request),
                        request.memory_bytes,
                        lease_seconds=self._reservation_seconds,
                    )
                    if not isinstance(lease, CapacityReservation):
                        raise DependencyUnavailable(
                            "worker returned an invalid deployment reservation"
                        )
                    if (
                        lease.job_id != job_id
                        or lease.memory_bytes != request.memory_bytes
                        or lease.state not in {"reserved", "dispatched"}
                    ):
                        raise DependencyUnavailable(
                            "worker deployment reservation identity does not match request"
                        )
                    if lease.state == "reserved":
                        capacity.dispatch_capacity(job_id, lease.token)
                        lease = replace(lease, state="dispatched")
                    leases.append(_LeaseBinding(capacity, lease))
            except Exception as exc:
                cleanup_errors: list[Exception] = []
                for capacity, job_id in reversed(attempted):
                    try:
                        capacity.release_capacity(job_id)
                    except Exception as cleanup_exc:  # pragma: no cover - defensive port fence
                        cleanup_errors.append(cleanup_exc)
                if cleanup_errors:
                    raise DependencyUnavailable(
                        "deployment admission cleanup is unresolved"
                    ) from cleanup_errors[0]
                if isinstance(exc, SonderError):
                    raise
                raise DependencyUnavailable("deployment resource admission failed") from exc

            receipt = DeploymentAdmissionReceipt(
                cluster_id=deployment.cluster_id,
                deployment_digest=deployment.digest,
                reservation_group=deployment.reservation_group,
                plan_digest=plan_digest,
                reservations=tuple(
                    DeploymentReservation(
                        rank=request.rank.rank,
                        host_id=request.rank.host_id,
                        worker_id=request.rank.worker_id,
                        device_id=request.rank.device_id,
                        job_id=binding.lease.job_id,
                        memory_bytes=binding.lease.memory_bytes,
                    )
                    for request, binding in zip(resources, leases)
                ),
            )
            self._active[key] = _ActiveAdmission(receipt, tuple(leases))
            return receipt

    def release(self, receipt: DeploymentAdmissionReceipt) -> DeploymentAdmissionReceipt:
        """Release a live receipt after worker cleanup proof is available."""

        if not isinstance(receipt, DeploymentAdmissionReceipt):
            raise TypeError("deployment admission receipt is invalid")
        key = self._receipt_key(receipt)
        with self._lock:
            active = self._active.get(key)
            if active is None or active.receipt != receipt:
                raise NotFound("deployment admission was not found")
            errors: list[Exception] = []
            for binding in reversed(active.leases):
                try:
                    binding.capacity.release_capacity(binding.lease.job_id)
                except Exception as exc:  # pragma: no cover - defensive port fence
                    errors.append(exc)
            if errors:
                raise DependencyUnavailable(
                    "deployment admission cleanup is unresolved"
                ) from errors[0]
            del self._active[key]
            return receipt

    @staticmethod
    def _receipt_key(receipt: DeploymentAdmissionReceipt) -> tuple[str, str]:
        # The active value still requires exact receipt equality before any
        # release call; the receipt's cluster/group pair only locates the
        # candidate entry.
        return (receipt.cluster_id, receipt.reservation_group)

    def reconcile(
        self,
        *,
        now: datetime | None = None,
        limit: int = _MAX_RECONCILIATION_ROWS,
    ) -> DeploymentReconciliation:
        """Delegate bounded worker reconciliation and project affected plans."""

        if type(limit) is not int or not 1 <= limit <= _MAX_RECONCILIATION_ROWS:
            raise ValueError(
                f"deployment reconciliation limit must be within 1..{_MAX_RECONCILIATION_ROWS}"
            )
        results: list[CapacityReconciliation] = []
        remaining = limit
        for capacity in self._capacity_targets():
            result = capacity.reconcile_capacity(now=now, limit=remaining)
            if not isinstance(result, CapacityReconciliation):
                raise DependencyUnavailable("worker returned invalid reconciliation evidence")
            results.append(result)
            remaining -= result.inspected
            if remaining <= 0:
                break
        if not results:
            raise DependencyUnavailable("worker capacity reconciliation is not configured")
        expired_job_ids = tuple(item.job_id for result in results for item in result.expired)
        expired = set(expired_job_ids)
        with self._lock:
            affected = tuple(
                sorted(
                    active.receipt.plan_digest
                    for active in self._active.values()
                    if any(binding.lease.job_id in expired for binding in active.leases)
                )
            )
        return DeploymentReconciliation(
            results[0].observed_at,
            expired_job_ids,
            affected,
            sum(result.inspected for result in results),
        )


__all__ = [
    "DeploymentAdmissionReceipt",
    "DeploymentAdmissionService",
    "DeploymentReconciliation",
    "DeploymentReservation",
    "DeploymentResourceRequest",
]
