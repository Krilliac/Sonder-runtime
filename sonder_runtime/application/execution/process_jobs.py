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
    deadline_seconds: int | None = None
    memory_limit_bytes: int | None = None
    metadata: tuple[tuple[str, str], ...] = ()
    require_job_scope: bool = False
    capacity_token: str | None = None

    def __post_init__(self) -> None:
        if self.capacity_token is not None and (
            not isinstance(self.capacity_token, str) or len(self.capacity_token) != 64
            or any(c not in "0123456789abcdef" for c in self.capacity_token)
        ):
            raise ValueError("capacity_token must be a 256-bit hex token")
        if not isinstance(self.identity, JobIdentity):
            raise TypeError("identity must be a JobIdentity")
        if not self.argv or any(not isinstance(item, str) or not item for item in self.argv):
            raise ValueError("argv must contain non-empty strings")
        if isinstance(self.max_descendants, bool) or self.max_descendants < 1:
            raise ValueError("max_descendants must be positive")
        if self.deadline_seconds is not None and (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, int)
            or not 1 <= self.deadline_seconds <= 86_400
        ):
            raise ValueError("deadline_seconds must be within 1..86400")
        if self.memory_limit_bytes is not None and (
            isinstance(self.memory_limit_bytes, bool)
            or not isinstance(self.memory_limit_bytes, int)
            or not 1 <= self.memory_limit_bytes <= 1 << 50
        ):
            raise ValueError("memory_limit_bytes must be within 1..2^50")
        if not isinstance(self.require_job_scope, bool):
            raise TypeError("require_job_scope must be a boolean")
        if any(not isinstance(key, str) or not key for key, _ in self.environment):
            raise ValueError("environment keys must be non-empty strings")
        seen: set[str] = set()
        for key, value in self.metadata:
            if (
                not isinstance(key, str)
                or not key
                or key in seen
                or not isinstance(value, str)
                or len(value) > 4096
                or "\x00" in value
            ):
                raise ValueError("metadata must contain unique bounded string pairs")
            seen.add(key)


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
