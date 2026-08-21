"""WP3-SEAM-010 application services for jobs and resumable workflows.

Services enforce the application contract and delegate durability to ports.
They intentionally do not know about existing persistence stores.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...domain.common.errors import ConcurrencyConflict, InvalidInput, NotFound
from ..jobs.durable_registry import (
    ProcessTreeCleanupContract,
    ProcessTreeCleanupReceipt,
    ProcessTreeCleanupRequest,
)
from ..ports.jobs import (
    JobClaim, JobIdentity, JobRecord, JobRegistry, JobStatus,
    WorkflowCheckpoint, WorkflowDefinition, WorkflowRepository, WorkflowResume,
)


@dataclass(frozen=True, slots=True)
class JobCancellationResult:
    """Cancellation plus the bounded process-cleanup proof, if requested."""

    records: tuple[JobRecord, ...]
    cleanup_receipts: tuple[ProcessTreeCleanupReceipt, ...] = ()
    cleanup_completed: bool = False
    detail: str = ""


class JobRegistryService:
    """Validate requests and expose a single leased job lifecycle."""

    def __init__(self, port: JobRegistry, *, process_cleanup: ProcessTreeCleanupContract | None = None) -> None:
        self._port = port
        self._process_cleanup = process_cleanup

    def create(self, identity: JobIdentity, *, metadata: dict[str, Any] | None = None) -> JobRecord:
        return self._port.create(identity, metadata=metadata)

    def get(self, job_id: str) -> JobRecord:
        job = self._port.get(job_id)
        if job is None:
            raise NotFound(f"job {job_id!r} not found")
        return job

    def list(self, *, include_terminal: bool = True, limit: int = 100) -> tuple[JobRecord, ...]:
        if limit < 1:
            raise InvalidInput("limit must be positive")
        return self._port.list(include_terminal=include_terminal, limit=limit)

    def cancel(self, job_id: str, reason: str = "cancelled") -> tuple[JobRecord, ...]:
        """Cancel through the durable adapter's optional capability."""
        if not isinstance(job_id, str) or not job_id.strip():
            raise InvalidInput("job_id is required")
        if not isinstance(reason, str) or not reason.strip():
            raise InvalidInput("cancellation reason is required")
        cancel = getattr(self._port, "cancel", None)
        if not callable(cancel):
            raise InvalidInput("job cancellation is not supported")
        try:
            records = cancel(job_id, reason=reason)
        except KeyError as exc:
            raise NotFound(f"job {job_id!r} not found") from exc
        return tuple(records)

    def cancel_with_cleanup(
        self,
        job_id: str,
        reason: str = "cancelled",
        *,
        process_cleanup: ProcessTreeCleanupContract | None = None,
        max_descendants: int = 64,
    ) -> JobCancellationResult:
        """Cancel descendants and request bounded cleanup for known processes.

        The existing ``cancel`` contract remains unchanged. Cleanup is an
        opt-in application capability: when no supervisor or process metadata
        is available, the result explicitly remains incomplete rather than
        claiming that an operating-system process tree was cleaned.
        """
        if isinstance(max_descendants, bool) or max_descendants < 1:
            raise InvalidInput("max_descendants must be positive")
        records = self.cancel(job_id, reason)
        supervisor = process_cleanup if process_cleanup is not None else self._process_cleanup
        if supervisor is None:
            return JobCancellationResult(records, detail="process cleanup contract is not configured")

        view = getattr(self._port, "view", None)
        if not callable(view):
            return JobCancellationResult(records, detail="job process metadata is not available")

        receipts: list[ProcessTreeCleanupReceipt] = []
        for record in records:
            try:
                metadata = view(record.identity.job_id)
                process_id = getattr(metadata, "process_id", None)
                if process_id is None:
                    return JobCancellationResult(
                        records, tuple(receipts), False,
                        "job process metadata does not contain a process id",
                    )
                receipt = supervisor.cleanup(ProcessTreeCleanupRequest(
                    record.identity.job_id,
                    process_id,
                    getattr(metadata, "process_group_id", None),
                    max_descendants,
                    reason,
                ))
                if not isinstance(receipt, ProcessTreeCleanupReceipt):
                    return JobCancellationResult(
                        records, tuple(receipts), False,
                        "process cleanup returned an invalid receipt",
                    )
                if receipt.job_id != record.identity.job_id:
                    return JobCancellationResult(
                        records, tuple(receipts), False,
                        "process cleanup receipt identified the wrong job",
                    )
                receipts.append(receipt)
                if not receipt.complete:
                    return JobCancellationResult(
                        records, tuple(receipts), False,
                        receipt.detail or "process-tree cleanup is incomplete",
                    )
            except Exception as exc:
                return JobCancellationResult(
                    records, tuple(receipts), False,
                    f"process-tree cleanup failed: {type(exc).__name__}",
                )
        return JobCancellationResult(records, tuple(receipts), True)

    def claim(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> JobClaim:
        if not worker_id.strip() or lease_seconds <= 0:
            raise InvalidInput("worker_id is required and lease_seconds must be positive")
        claim = self._port.claim(job_id, worker_id, lease_seconds=lease_seconds)
        if claim is None:
            raise ConcurrencyConflict(f"job {job_id!r} could not be claimed")
        return claim

    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> None:
        if not self._port.heartbeat(job_id, worker_id, lease_seconds=lease_seconds):
            raise ConcurrencyConflict(f"job {job_id!r} heartbeat rejected")

    def finish(self, job_id: str, worker_id: str, status: JobStatus, *, result: Any = None, error: str = "") -> JobRecord:
        if status not in {JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED}:
            raise InvalidInput("finish requires a terminal job status")
        job = self._port.finish(job_id, worker_id, status, result=result, error=error)
        if job is None:
            raise ConcurrencyConflict(f"job {job_id!r} finish rejected")
        return job

    def reconcile(self, *, now: str | None = None) -> int:
        return self._port.reconcile(now=now)


class ResumableWorkflowEngine:
    """Workflow protocol backed by a job registry and monotonic checkpoints."""

    def __init__(self, jobs: JobRegistryService, checkpoints: WorkflowRepository) -> None:
        self._jobs = jobs
        self._checkpoints = checkpoints

    def start(self, identity: JobIdentity, definition: WorkflowDefinition) -> JobRecord:
        if identity.kind != "workflow":
            raise InvalidInput("workflow jobs must use identity.kind='workflow'")
        job = self._jobs.create(identity, metadata={"workflow_id": definition.workflow_id, "version": definition.version})
        checkpoint = WorkflowCheckpoint(job.identity.job_id, 0, 0)
        saved = self._checkpoints.save_checkpoint(checkpoint, expected_sequence=-1)
        if saved is None:
            raise ConcurrencyConflict(f"workflow {job.identity.job_id!r} checkpoint already exists")
        return job

    def resume(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> WorkflowResume:
        self._jobs.claim(job_id, worker_id, lease_seconds=lease_seconds)
        job = self._jobs.get(job_id)
        checkpoint = self._checkpoints.get_checkpoint(job_id)
        if checkpoint is None:
            raise NotFound(f"workflow checkpoint for {job_id!r} not found")
        return WorkflowResume(job, checkpoint)

    def checkpoint(self, job_id: str, worker_id: str, *, next_step: int, state: dict[str, Any], completed_step_id: str | None = None) -> WorkflowCheckpoint:
        self._jobs.heartbeat(job_id, worker_id)
        current = self._checkpoints.get_checkpoint(job_id)
        if current is None:
            raise NotFound(f"workflow checkpoint for {job_id!r} not found")
        if next_step < current.next_step:
            raise InvalidInput("workflow checkpoints cannot move backwards")
        candidate = WorkflowCheckpoint(job_id, current.sequence + 1, next_step, dict(state), completed_step_id)
        saved = self._checkpoints.save_checkpoint(candidate, expected_sequence=current.sequence)
        if saved is None:
            raise ConcurrencyConflict(f"workflow {job_id!r} checkpoint conflict")
        return saved

    def finish(self, job_id: str, worker_id: str, status: JobStatus, *, result: Any = None, error: str = "") -> JobRecord:
        return self._jobs.finish(job_id, worker_id, status, result=result, error=error)


__all__ = ["JobCancellationResult", "JobRegistryService", "ResumableWorkflowEngine"]
