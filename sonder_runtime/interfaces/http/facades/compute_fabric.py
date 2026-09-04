"""Bounded HTTP projections for compute snapshots and catalog jobs."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

logger = logging.getLogger(__name__)

from ....application.compute_fabric.jobs import (
    ComputeJobWorker,
    RemoteArtifactPayload,
    RemoteJobReceipt,
)
from ....application.compute_fabric.wire import (
    job_envelope_from_wire,
    job_receipt_to_wire,
    snapshot_to_wire,
)


@dataclass(frozen=True, slots=True)
class ComputeFabricHttpResult:
    body: dict[str, Any]
    status_code: int = 200


@dataclass(frozen=True, slots=True)
class ComputeArtifactHttpResult:
    payload: RemoteArtifactPayload
    status_code: int = 200


def dispatch_compute_snapshot(
    snapshot_factory: Callable[[], Any],
) -> ComputeFabricHttpResult:
    logger.debug("dispatch_compute_snapshot: generating snapshot")
    snapshot = snapshot_factory()
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
        logger.error(f"compute worker returned unexpected receipt type: {type(receipt).__name__}")
        raise TypeError("compute worker returned an invalid receipt")
    return ComputeFabricHttpResult(
        {"object": "compute_job", "job": job_receipt_to_wire(receipt)},
        status_code=status_code,
    )


def dispatch_compute_job_submit(
    worker: ComputeJobWorker,
    payload: dict[str, Any],
) -> ComputeFabricHttpResult:
    logger.info(f"Compute job submitted")
    logger.debug("dispatch_compute_job_submit: submitting job")
    return _job_result(
        worker.submit(job_envelope_from_wire(payload)),
        status_code=202,
    )


def dispatch_compute_job_status(
    worker: ComputeJobWorker,
    remote_job_id: str,
) -> ComputeFabricHttpResult:
    logger.debug(f"dispatch_compute_job_status: remote_job_id={remote_job_id!r}")
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
    logger.info(f"Compute job cancelled, remote_job_id={remote_job_id!r}")
    logger.debug(f"dispatch_compute_job_cancel: remote_job_id={remote_job_id!r}, reason={reason!r}")
    return _job_result(worker.cancel(remote_job_id, reason=reason))


def dispatch_compute_job_artifact(
    worker: ComputeJobWorker,
    remote_job_id: str,
    name: str,
) -> ComputeArtifactHttpResult:
    logger.debug(f"dispatch_compute_job_artifact: remote_job_id={remote_job_id!r}, name={name!r}")
    return ComputeArtifactHttpResult(worker.read_artifact(remote_job_id, name))


__all__ = [
    "ComputeFabricHttpResult",
    "ComputeArtifactHttpResult",
    "dispatch_compute_job_artifact",
    "dispatch_compute_job_by_idempotency",
    "dispatch_compute_job_cancel",
    "dispatch_compute_job_status",
    "dispatch_compute_job_submit",
    "dispatch_compute_snapshot",
]
