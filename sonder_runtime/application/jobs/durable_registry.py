"""Parent-linked durable job registry (REMAINING-JOB-002-004).

The registry is the application seam shared by generic jobs, workflows, and
execution-world adapters.  It keeps immutable job records, bounded output,
and restart observations together; a persistence adapter can replace the
reference store without changing the lifecycle contract.  Process cleanup is
an explicit platform contract.  This module creates and validates cleanup
requests but never claims to terminate an operating-system process itself.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Any, Callable, Protocol

from ..execution.world_control import (
    BoundedOutputBuffer,
    OutputPage,
    OutputStream,
    OutputWatermark,
    SpillReference,
)
from ..operations.startup_reconciliation import (
    DrainAction,
    DrainPlan,
    ProcessTreeCleanupIntent,
    RecordKind,
    StartupObservation,
    build_drain_plan,
)
from ..ports.jobs import JobIdentity, JobRecord, JobStatus, TERMINAL_JOB_STATUSES


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ProcessTreeCleanupRequest:
    """Bounded request handed to a platform process supervisor."""

    job_id: str
    process_id: int
    process_group_id: int | None
    max_descendants: int = 64
    reason: str = "job cancelled"

    def __post_init__(self) -> None:
        if not self.job_id.strip() or self.process_id <= 0:
            raise ValueError("job_id and positive process_id are required")
        if self.process_group_id is not None and self.process_group_id <= 0:
            raise ValueError("process_group_id must be positive")
        if isinstance(self.max_descendants, bool) or self.max_descendants < 1:
            raise ValueError("max_descendants must be positive")
        if not self.reason.strip():
            raise ValueError("cleanup reason is required")


@dataclass(frozen=True, slots=True)
class ProcessTreeCleanupReceipt:
    """Truthful result returned by a platform cleanup adapter."""

    job_id: str
    requested: bool
    descendants_seen: int = 0
    descendants_terminated: int = 0
    complete: bool = False
    detail: str = ""

    def __post_init__(self) -> None:
        if self.descendants_seen < 0 or self.descendants_terminated < 0:
            raise ValueError("cleanup counts cannot be negative")
        if self.descendants_terminated > self.descendants_seen:
            raise ValueError("terminated descendants cannot exceed seen descendants")
        if self.complete and not self.requested:
            raise ValueError("complete cleanup requires a requested cleanup")
        if self.complete and self.descendants_terminated != self.descendants_seen:
            raise ValueError("complete cleanup requires every seen descendant terminated")


@dataclass(frozen=True, slots=True)
class JobRecoveryReport:
    """Typed result of bounded restart reconciliation and cleanup."""

    plan: DrainPlan
    cleanup_receipts: tuple[ProcessTreeCleanupReceipt, ...]
    interrupted_job_ids: tuple[str, ...]


class ProcessTreeCleanupContract(Protocol):
    """Platform-owned process-tree cleanup operation."""

    def cleanup(self, request: ProcessTreeCleanupRequest) -> ProcessTreeCleanupReceipt: ...


@dataclass(frozen=True, slots=True)
class DurableJobView:
    """A registry record plus bounded execution metadata."""

    record: JobRecord
    parent_job_id: str | None
    child_job_ids: tuple[str, ...]
    process_id: int | None = None
    process_group_id: int | None = None


class DurableJobRegistry:
    """Thread-safe reference registry for all durable job surfaces.

    The reference implementation is intentionally in-memory, but its public
    operations use immutable records and monotonic revisions so a durable
    adapter can implement the same compare-and-set behavior.  A cancelled
    parent propagates cancellation to every descendant in stable order.
    """

    def __init__(self, *, clock: Callable[[], str] = _now, output_bounds: tuple[int, int] = (256, 64 * 1024)) -> None:
        max_events, max_bytes = output_bounds
        if max_events < 1 or max_bytes < 1:
            raise ValueError("output bounds must be positive")
        self._clock = clock
        self._max_events = max_events
        self._max_bytes = max_bytes
        self._records: dict[str, JobRecord] = {}
        self._children: dict[str, list[str]] = {}
        self._outputs: dict[str, BoundedOutputBuffer] = {}
        self._processes: dict[str, tuple[int, int | None]] = {}
        self._lock = RLock()

    def start(
        self,
        identity: JobIdentity,
        *,
        parent_job_id: str | None = None,
        process_id: int | None = None,
        process_group_id: int | None = None,
    ) -> JobRecord:
        """Create one pending job and validate its parent before publication."""
        if not isinstance(identity, JobIdentity):
            raise TypeError("identity must be a JobIdentity")
        parent = parent_job_id if parent_job_id is not None else identity.parent_job_id
        if parent == identity.job_id:
            raise ValueError("a job cannot be its own parent")
        with self._lock:
            if identity.job_id in self._records:
                raise ValueError(f"job {identity.job_id!r} already exists")
            if parent is not None and parent not in self._records:
                raise KeyError(f"parent job {parent!r} not found")
            if process_id is not None and process_id <= 0:
                raise ValueError("process_id must be positive")
            if process_group_id is not None and process_group_id <= 0:
                raise ValueError("process_group_id must be positive")
            if parent != identity.parent_job_id:
                identity = replace(identity, parent_job_id=parent)
            now = self._clock()
            record = JobRecord(identity, JobStatus.PENDING, 0, now, now)
            self._records[identity.job_id] = record
            self._children.setdefault(identity.job_id, [])
            if parent is not None:
                self._children.setdefault(parent, []).append(identity.job_id)
            self._outputs[identity.job_id] = BoundedOutputBuffer(
                max_events=self._max_events, max_bytes=self._max_bytes
            )
            if process_id is not None:
                self._processes[identity.job_id] = (process_id, process_group_id)
            return record

    def list(
        self,
        *,
        parent_job_id: str | None = None,
        include_terminal: bool = True,
        limit: int = 100,
    ) -> tuple[JobRecord, ...]:
        """Return a bounded, stable listing optionally scoped to one parent."""
        if isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be positive")
        with self._lock:
            ids = tuple(self._children.get(parent_job_id, ())) if parent_job_id is not None else tuple(self._records)
            values = tuple(self._records[job_id] for job_id in ids)
            if parent_job_id is None:
                values = tuple(self._records.values())
            if not include_terminal:
                values = tuple(record for record in values if record.status not in TERMINAL_JOB_STATUSES)
            return values[:limit]

    def poll(self, job_id: str) -> JobRecord:
        with self._lock:
            try:
                return self._records[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job {job_id!r}") from exc

    def view(self, job_id: str) -> DurableJobView:
        with self._lock:
            record = self.poll(job_id)
            process = self._processes.get(job_id)
            return DurableJobView(
                record,
                record.identity.parent_job_id,
                tuple(self._children.get(job_id, ())),
                *(process or (None, None)),
            )

    def stream(
        self,
        job_id: str,
        *,
        after: OutputWatermark | None = None,
        max_events: int = 64,
        max_bytes: int = 16 * 1024,
    ) -> OutputPage:
        with self._lock:
            self.poll(job_id)
            return self._outputs[job_id].read(after, max_events=max_events, max_bytes=max_bytes)

    def append_output(
        self,
        job_id: str,
        stream: OutputStream,
        data: str,
        *,
        spill: SpillReference | None = None,
    ) -> None:
        with self._lock:
            self.poll(job_id)
            self._outputs[job_id].append(stream, data, spill=spill)

    def transition(
        self,
        job_id: str,
        status: JobStatus,
        *,
        result: Any = None,
        error: str = "",
    ) -> JobRecord:
        """Publish a monotonic state transition for an adapter/worker."""
        if not isinstance(status, JobStatus):
            raise TypeError("status must be a JobStatus")
        with self._lock:
            current = self.poll(job_id)
            if current.is_terminal:
                return current
            if status is JobStatus.SUCCEEDED and error:
                raise ValueError("successful jobs cannot carry an error")
            now = self._clock()
            updated = replace(current, status=status, revision=current.revision + 1, updated_at=now, result=result, error=error)
            self._records[job_id] = updated
            return updated

    def cancel(self, job_id: str, *, reason: str = "cancelled") -> tuple[JobRecord, ...]:
        """Cancel a job and all descendants, returning stable changed records."""
        if not reason.strip():
            raise ValueError("cancellation reason is required")
        with self._lock:
            self.poll(job_id)
            ids: list[str] = []
            queue = [job_id]
            while queue:
                current_id = queue.pop(0)
                ids.append(current_id)
                queue.extend(self._children.get(current_id, ()))
            changed: list[JobRecord] = []
            for current_id in ids:
                current = self._records[current_id]
                if current.is_terminal:
                    changed.append(current)
                    continue
                changed.append(self.transition(current_id, JobStatus.CANCELLED, error=reason))
            return tuple(changed)

    def collect(self, job_id: str) -> JobRecord:
        record = self.poll(job_id)
        if not record.is_terminal:
            raise ValueError("job is not terminal")
        return record

    def reconcile(
        self,
        *,
        owner_instance_id: str = "",
        owner_alive: bool | None = None,
        max_records: int = 100,
        max_process_descendants: int = 64,
    ) -> DrainPlan:
        """Classify active records after restart and return bounded cleanup intents."""
        with self._lock:
            observations = []
            for record in self._records.values():
                process = self._processes.get(record.identity.job_id)
                observations.append(StartupObservation(
                    RecordKind.JOB,
                    record.identity.job_id,
                    record.status.value,
                    owner_instance_id=owner_instance_id,
                    owner_alive=owner_alive,
                    checkpoint_available=record.status in {JobStatus.PAUSED, JobStatus.INTERRUPTED},
                    retryable=record.status in {JobStatus.PENDING, JobStatus.PAUSED, JobStatus.INTERRUPTED},
                    process_id=process[0] if process else None,
                    process_group_id=process[1] if process else None,
                ))
            plan = build_drain_plan(observations, max_records=max_records, max_process_descendants=max_process_descendants)
            for result in plan.results:
                if result.action is DrainAction.MARK_INTERRUPTED:
                    current = self._records[result.observation.record_id]
                    if not current.is_terminal and current.status is not JobStatus.INTERRUPTED:
                        self._records[current.identity.job_id] = replace(
                            current, status=JobStatus.INTERRUPTED, revision=current.revision + 1, updated_at=self._clock()
                        )
            return plan

    @staticmethod
    def cleanup_request(intent: ProcessTreeCleanupIntent) -> ProcessTreeCleanupRequest:
        if intent.process_id <= 0:
            raise ValueError("cleanup intent must contain a positive process id")
        return ProcessTreeCleanupRequest(
            intent.record_id, intent.process_id, intent.process_group_id,
            intent.max_descendants, intent.reason,
        )


__all__ = [
    "DurableJobRegistry", "DurableJobView", "JobRecoveryReport", "ProcessTreeCleanupContract",
    "ProcessTreeCleanupReceipt", "ProcessTreeCleanupRequest",
]
