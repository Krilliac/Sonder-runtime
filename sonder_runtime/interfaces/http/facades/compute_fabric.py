"""Bounded HTTP projections for compute snapshots and catalog jobs."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ....application.compute_fabric.jobs import ComputeJobWorker, RemoteJobReceipt
from ....application.compute_fabric.wire import (
    job_envelope_from_wire,
    job_receipt_to_wire,
    snapshot_to_wire,
)
from ....domain.compute_fabric import NodeSnapshot


@dataclass(frozen=True, slots=True)
class ComputeFabricHttpResult:
    body: dict[str, Any]
    status_code: int = 200


def dispatch_compute_snapshot(
    snapshot_factory: Callable[[], NodeSnapshot],
) -> ComputeFabricHttpResult:
    snapshot = snapshot_factory()
    if not isinstance(snapshot, NodeSnapshot):
        raise TypeError("compute snapshot factory returned an invalid value")
    return ComputeFabricHttpResult({
        "object": "compute_snapshot",
        "snapshot": snapshot_to_wire(snapshot),
    })


def _job_result(
    receipt: RemoteJobReceipt,
    *,
    status_code: int = 200,
) -> ComputeFabricHttpResult:
    if not isinstance(receipt, RemoteJobReceipt):
        raise TypeError("compute worker returned an invalid receipt")
    return ComputeFabricHttpResult(
        {"object": "compute_job", "job": job_receipt_to_wire(receipt)},
        status_code=status_code,
    )


def dispatch_compute_job_submit(
    worker: ComputeJobWorker,
    payload: dict[str, Any],
) -> ComputeFabricHttpResult:
    return _job_result(
        worker.submit(job_envelope_from_wire(payload)),
        status_code=202,
    )


def dispatch_compute_job_status(
    worker: ComputeJobWorker,
    remote_job_id: str,
) -> ComputeFabricHttpResult:
    return _job_result(worker.status(remote_job_id))


def dispatch_compute_job_by_idempotency(
    worker: ComputeJobWorker,
    idempotency_key: str,
) -> ComputeFabricHttpResult | None:
    receipt = worker.by_idempotency(idempotency_key)
    return None if receipt is None else _job_result(receipt)


def dispatch_compute_job_cancel(
    worker: ComputeJobWorker,
    remote_job_id: str,
    reason: str,
) -> ComputeFabricHttpResult:
    return _job_result(worker.cancel(remote_job_id, reason=reason))


__all__ = [
    "ComputeFabricHttpResult",
    "dispatch_compute_job_by_idempotency",
    "dispatch_compute_job_cancel",
    "dispatch_compute_job_status",
    "dispatch_compute_job_submit",
    "dispatch_compute_snapshot",
]
