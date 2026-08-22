"""Typed contract for one process-backed durable execution path (JOB-004)."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..jobs.durable_registry import ProcessTreeCleanupContract
from ..ports.jobs import JobIdentity, JobRecord


@dataclass(frozen=True, slots=True)
class ProcessJobRequest:
    """Bounded argv request mapped to one durable job."""

    identity: JobIdentity
    argv: tuple[str, ...]
    cwd: Path | None = None
    environment: tuple[tuple[str, str], ...] = ()
    max_descendants: int = 64

    def __post_init__(self) -> None:
        if not isinstance(self.identity, JobIdentity):
            raise TypeError("identity must be a JobIdentity")
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("argv must contain non-empty strings")
        if isinstance(self.max_descendants, bool) or self.max_descendants < 1:
            raise ValueError("max_descendants must be positive")
        if any(not isinstance(key, str) or not key for key, _ in self.environment):
            raise ValueError("environment keys must be non-empty strings")


@dataclass(frozen=True, slots=True)
class ProcessJobStart:
    """Proof that a launched process was registered with its durable job."""

    record: JobRecord
    process_id: int
    process_group_id: int | None


@dataclass(frozen=True, slots=True)
class ProcessJobWait:
    """Bounded wait result; ``timed_out`` never claims a terminal job."""

    record: JobRecord
    exit_code: int | None
    timed_out: bool = False


class ProcessJobProvider(Protocol):
    """Concrete provider seam used by the process-backed adapter."""

    def start(self, request: ProcessJobRequest) -> ProcessJobStart: ...

    def wait(self, job_id: str, *, timeout: float | None = None) -> ProcessJobWait: ...

    def cancel(self, job_id: str, reason: str = "cancelled"):
        """Return a ``JobCancellationResult`` with cleanup truth."""


__all__ = ["ProcessJobProvider", "ProcessJobRequest", "ProcessJobStart", "ProcessJobWait"]
