"""WP3-SEAM-010 application ports for durable jobs and resumable workflows.

The types in this module are the persistence-neutral contract.  An adapter may
back them with SQLite, a remote queue, or another durable store; this seam does
not open connections or alter any existing store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


MAX_JOB_ATTEMPTS = 100


class JobStatus(str, Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    PAUSED = "paused"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"


TERMINAL_JOB_STATUSES = frozenset({
    JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED,
})


@dataclass(frozen=True)
class JobIdentity:
    """Stable identity that survives process restarts and retries."""

    job_id: str
    kind: str
    operation_id: str
    idempotency_key: str
    parent_job_id: str | None = None
    parent_session_id: str | None = None

    def __post_init__(self) -> None:
        for name in ("job_id", "kind", "operation_id", "idempotency_key"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class JobRecord:
    identity: JobIdentity
    status: JobStatus = JobStatus.PENDING
    revision: int = 0
    created_at: str = ""
    updated_at: str = ""
    result: Any = None
    error: str = ""
    attempt: int = 0
    max_attempts: int = 3

    def __post_init__(self) -> None:
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise ValueError("job attempt cannot be negative")
        if (isinstance(self.max_attempts, bool) or not isinstance(self.max_attempts, int)
                or not 1 <= self.max_attempts <= MAX_JOB_ATTEMPTS):
            raise ValueError(f"job max_attempts must be between 1 and {MAX_JOB_ATTEMPTS}")
        if self.attempt > self.max_attempts:
            raise ValueError("job attempt cannot exceed max_attempts")

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


@dataclass(frozen=True)
class JobClaim:
    job_id: str
    worker_id: str
    lease_until: str
    revision: int
    claim_token: str = ""
    attempt: int = 0

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.worker_id.strip():
            raise ValueError("claim job_id and worker_id must be non-empty")
        if isinstance(self.attempt, bool) or not isinstance(self.attempt, int) or self.attempt < 0:
            raise ValueError("claim attempt cannot be negative")


@dataclass(frozen=True)
class JobCompletionReceipt:
    """Exactly-once commitment proof for one durable job attempt."""

    job_id: str
    attempt: int
    receipt_key: str
    status: JobStatus
    payload_digest: str
    committed_at: str
    revision: int

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.receipt_key.strip():
            raise ValueError("receipt job_id and receipt_key must be non-empty")
        if self.attempt < 1:
            raise ValueError("receipt attempt must be positive")
        if self.status not in TERMINAL_JOB_STATUSES:
            raise ValueError("completion receipt status must be terminal")
        if (len(self.payload_digest) != 64
                or any(character not in "0123456789abcdef" for character in self.payload_digest)):
            raise ValueError("completion receipt payload_digest must be sha256")
        if not self.committed_at:
            raise ValueError("completion receipt committed_at must be non-empty")
        if self.revision < 1:
            raise ValueError("completion receipt revision must be positive")


@dataclass(frozen=True)
class JobReconciliationReport:
    """Bounded stale-lease reconciliation diagnostics."""

    scanned: int
    interrupted_job_ids: tuple[str, ...]
    truncated: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.scanned, bool) or self.scanned < 0:
            raise ValueError("reconciliation scanned count cannot be negative")
        if len(self.interrupted_job_ids) > self.scanned:
            raise ValueError("interrupted jobs cannot exceed scanned jobs")
        if len(set(self.interrupted_job_ids)) != len(self.interrupted_job_ids):
            raise ValueError("interrupted job IDs must be unique")

    @property
    def interrupted(self) -> int:
        return len(self.interrupted_job_ids)


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    name: str
    payload: Any = None

    def __post_init__(self) -> None:
        if not self.step_id.strip() or not self.name.strip():
            raise ValueError("workflow step_id and name must be non-empty")


@dataclass(frozen=True)
class WorkflowDefinition:
    workflow_id: str
    steps: tuple[WorkflowStep, ...]
    version: str = "1"

    def __post_init__(self) -> None:
        if not self.workflow_id.strip() or not self.version.strip():
            raise ValueError("workflow_id and version must be non-empty")
        ids = [step.step_id for step in self.steps]
        if len(ids) != len(set(ids)):
            raise ValueError("workflow step_id values must be unique")


@dataclass(frozen=True)
class WorkflowCheckpoint:
    job_id: str
    sequence: int
    next_step: int
    state: dict[str, Any] = field(default_factory=dict)
    completed_step_id: str | None = None

    def __post_init__(self) -> None:
        if self.sequence < 0 or self.next_step < 0:
            raise ValueError("checkpoint sequence and next_step cannot be negative")


@dataclass(frozen=True)
class WorkflowResume:
    job: JobRecord
    checkpoint: WorkflowCheckpoint
    claim: JobClaim | None = None


class JobRegistry(Protocol):
    """Durable job lifecycle; claim and writes are compare-and-set operations."""

    def create(self, identity: JobIdentity, *, metadata: dict[str, Any] | None = None) -> JobRecord: ...
    def get(self, job_id: str) -> JobRecord | None: ...
    def list(self, *, parent_job_id: str | None = None, include_terminal: bool = True,
             limit: int = 100) -> tuple[JobRecord, ...]: ...
    def cancel(self, job_id: str, *, reason: str = "cancelled",
               max_descendants: int = 256) -> tuple[JobRecord, ...]: ...
    def claim(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> JobClaim | None: ...
    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 300,
                  claim_token: str | None = None) -> bool: ...
    def finish(self, job_id: str, worker_id: str, status: JobStatus, *, result: Any = None,
               error: str = "", claim_token: str | None = None) -> JobRecord | None: ...
    def retry(self, job_id: str, *, expected_revision: int | None = None) -> JobRecord | None: ...
    def reconcile(self, *, now: str | None = None, max_records: int = 100) -> int: ...


class WorkflowRepository(Protocol):
    """Durable checkpoint storage owned by a future adapter."""

    def get_checkpoint(self, job_id: str) -> WorkflowCheckpoint | None: ...
    def save_checkpoint(self, checkpoint: WorkflowCheckpoint, *, expected_sequence: int) -> WorkflowCheckpoint | None: ...


class WorkflowEngine(Protocol):
    """Protocol for starting, resuming, and checkpointing workflow jobs."""

    def start(self, identity: JobIdentity, definition: WorkflowDefinition) -> JobRecord: ...
    def resume(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> WorkflowResume | None: ...
    def checkpoint(self, job_id: str, worker_id: str, *, next_step: int, state: dict[str, Any], completed_step_id: str | None = None,
                   claim_token: str | None = None) -> WorkflowCheckpoint: ...
    def finish(self, job_id: str, worker_id: str, status: JobStatus, *, result: Any = None,
               error: str = "", claim_token: str | None = None) -> JobRecord | None: ...


__all__ = [
    "JobClaim", "JobCompletionReceipt", "JobIdentity", "JobReconciliationReport",
    "JobRecord", "JobRegistry", "JobStatus",
    "MAX_JOB_ATTEMPTS", "TERMINAL_JOB_STATUSES", "WorkflowCheckpoint", "WorkflowDefinition",
    "WorkflowEngine", "WorkflowRepository", "WorkflowResume", "WorkflowStep",
]
