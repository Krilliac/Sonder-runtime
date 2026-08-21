"""Typed dependency-aware generic jobs (WP5-JOB-001).

This module is deliberately persistence- and transport-neutral.  A workflow
adapter can translate the execution records into the durable WP3 job port,
while callers get deterministic ordering and a small, typed execution API.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Generic, Mapping, Protocol, TypeVar


T = TypeVar("T")


class JobExecutionError(RuntimeError):
    """Raised when a generic job graph cannot be executed."""


class GenericJobStatus(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class RetryPolicy:
    """Bound the number of attempts; the hook decides whether to continue."""

    max_attempts: int = 1

    def __post_init__(self) -> None:
        if isinstance(self.max_attempts, bool) or self.max_attempts < 1:
            raise ValueError("max_attempts must be a positive integer")


@dataclass(frozen=True)
class ExecutionContext:
    """Read-only inputs made available to a job handler."""

    job_id: str
    attempt: int
    dependencies: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class ExecutionRecord(Generic[T]):
    job_id: str
    attempt: int
    status: GenericJobStatus
    value: T | None = None
    error: str = ""


@dataclass(frozen=True)
class GenericJob(Generic[T]):
    """A typed unit of work and the ids it requires before execution."""

    job_id: str
    handler: Callable[[ExecutionContext], T]
    dependencies: tuple[str, ...] = ()
    retry_policy: RetryPolicy = RetryPolicy()

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("job_id must be non-empty")
        if not callable(self.handler):
            raise TypeError("handler must be callable")
        if len(self.dependencies) != len(set(self.dependencies)):
            raise ValueError("dependencies must be unique")
        if self.job_id in self.dependencies:
            raise ValueError("a job cannot depend on itself")


class RetryHook(Protocol):
    def __call__(self, record: ExecutionRecord[object], error: Exception) -> bool: ...


class GenericJobExecutor:
    """Execute a finite job graph in stable dependency order."""

    def __init__(self, jobs: Mapping[str, GenericJob[object]], *, retry_hook: RetryHook | None = None) -> None:
        self._jobs = dict(jobs)
        self._retry_hook = retry_hook
        mismatched = sorted(
            key for key, job in self._jobs.items() if key != job.job_id
        )
        if mismatched:
            raise ValueError(
                "job mapping keys must match GenericJob.job_id: "
                + ", ".join(mismatched)
            )
        missing = sorted({dep for job in self._jobs.values() for dep in job.dependencies if dep not in self._jobs})
        if missing:
            raise ValueError(f"unknown job dependencies: {', '.join(missing)}")

    def order(self) -> tuple[str, ...]:
        """Return stable topological order, or reject cycles."""
        remaining = {job_id: set(job.dependencies) for job_id, job in self._jobs.items()}
        ordered: list[str] = []
        while remaining:
            ready = sorted(job_id for job_id, deps in remaining.items() if not deps)
            if not ready:
                raise JobExecutionError("job dependency graph contains a cycle")
            ordered.extend(ready)
            for job_id in ready:
                remaining.pop(job_id)
            for deps in remaining.values():
                deps.difference_update(ready)
        return tuple(ordered)

    def run(self) -> tuple[ExecutionRecord[object], ...]:
        records: list[ExecutionRecord[object]] = []
        values: dict[str, object] = {}
        for job_id in self.order():
            job = self._jobs[job_id]
            blocked = [dep for dep in job.dependencies if not any(r.job_id == dep and r.status is GenericJobStatus.SUCCEEDED for r in records)]
            if blocked:
                records.append(ExecutionRecord(job_id, 0, GenericJobStatus.BLOCKED, error=f"dependencies failed: {', '.join(blocked)}"))
                continue
            for attempt in range(1, job.retry_policy.max_attempts + 1):
                context = ExecutionContext(job_id, attempt, {dep: values[dep] for dep in job.dependencies})
                try:
                    value = job.handler(context)
                except Exception as error:  # handler failures become records, never graph corruption
                    record = ExecutionRecord(job_id, attempt, GenericJobStatus.FAILED, error=str(error))
                    records.append(record)
                    if attempt >= job.retry_policy.max_attempts or self._retry_hook is None or not self._retry_hook(record, error):
                        break
                else:
                    record = ExecutionRecord(job_id, attempt, GenericJobStatus.SUCCEEDED, value=value)
                    records.append(record)
                    values[job_id] = value
                    break
        return tuple(records)


__all__ = [
    "ExecutionContext", "ExecutionRecord", "GenericJob", "GenericJobExecutor",
    "GenericJobStatus", "JobExecutionError", "RetryHook", "RetryPolicy",
]
