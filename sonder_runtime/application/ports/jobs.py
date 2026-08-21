"""WP3-SEAM-010 application ports for durable jobs and resumable workflows.

The types in this module are the persistence-neutral contract.  An adapter may
back them with SQLite, a remote queue, or another durable store; this seam does
not open connections or alter any existing store.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


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

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_JOB_STATUSES


@dataclass(frozen=True)
class JobClaim:
    job_id: str
    worker_id: str
    lease_until: str
    revision: int


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


class JobRegistry(Protocol):
    """Durable job lifecycle; claim and writes are compare-and-set operations."""

    def create(self, identity: JobIdentity, *, metadata: dict[str, Any] | None = None) -> JobRecord: ...
    def get(self, job_id: str) -> JobRecord | None: ...
    def list(self, *, include_terminal: bool = True, limit: int = 100) -> tuple[JobRecord, ...]: ...
    def cancel(self, job_id: str, *, reason: str = "cancelled") -> tuple[JobRecord, ...]: ...
    def claim(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> JobClaim | None: ...
    def heartbeat(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> bool: ...
    def finish(self, job_id: str, worker_id: str, status: JobStatus, *, result: Any = None, error: str = "") -> JobRecord | None: ...
    def reconcile(self, *, now: str | None = None) -> int: ...


class WorkflowRepository(Protocol):
    """Durable checkpoint storage owned by a future adapter."""

    def get_checkpoint(self, job_id: str) -> WorkflowCheckpoint | None: ...
    def save_checkpoint(self, checkpoint: WorkflowCheckpoint, *, expected_sequence: int) -> WorkflowCheckpoint | None: ...


class WorkflowEngine(Protocol):
    """Protocol for starting, resuming, and checkpointing workflow jobs."""

    def start(self, identity: JobIdentity, definition: WorkflowDefinition) -> JobRecord: ...
    def resume(self, job_id: str, worker_id: str, *, lease_seconds: int = 300) -> WorkflowResume | None: ...
    def checkpoint(self, job_id: str, worker_id: str, *, next_step: int, state: dict[str, Any], completed_step_id: str | None = None) -> WorkflowCheckpoint: ...
    def finish(self, job_id: str, worker_id: str, status: JobStatus, *, result: Any = None, error: str = "") -> JobRecord | None: ...


__all__ = [
    "JobClaim", "JobIdentity", "JobRecord", "JobRegistry", "JobStatus",
    "TERMINAL_JOB_STATUSES", "WorkflowCheckpoint", "WorkflowDefinition",
    "WorkflowEngine", "WorkflowRepository", "WorkflowResume", "WorkflowStep",
]
