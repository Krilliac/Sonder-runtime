"""Worker-owned capacity, separate from process limits and placement telemetry."""
from datetime import datetime, timezone
from typing import Protocol

from ...domain.common.errors import DependencyUnavailable
from ...domain.compute_fabric import NodeSnapshot
from ...domain.worker_capacity import (
    CapacityReconciliation,
    CapacityReservation,
    CapacityReservationView,
    WorkerBudget,
    bounded_positive,
)


class WorkerCapacity(Protocol):
    def reserve_capacity(self, budget: WorkerBudget, job_id: str, request_sha256: str,
                         memory_bytes: int | None, *, lease_seconds: int = 30) -> CapacityReservation: ...
    def dispatch_capacity(self, job_id: str, token: str) -> None: ...
    def release_capacity(self, job_id: str) -> None: ...
    def reconcile_capacity(self, *, now: datetime | None = None, limit: int = 1024) -> CapacityReconciliation: ...
    def list_capacity(self, *, host_id: str | None = None, include_released: bool = False,
                      limit: int = 256) -> tuple[CapacityReservationView, ...]: ...


def measured_worker_budget(snapshot: NodeSnapshot, host_id: str, *, now: datetime | None = None) -> WorkerBudget:
    """Use a newly probed local RAM observation; dynamic mode is exclusive."""
    now = now or datetime.now(timezone.utc)
    age = (now - snapshot.observed_at).total_seconds()
    memory = snapshot.resources.free_ram_bytes
    if not snapshot.node.local or not 0 <= age <= 5 or memory is None:
        raise DependencyUnavailable("fresh local available RAM measurement is unavailable")
    return WorkerBudget(host_id, memory, 1)
