"""Durable worker launch and terminal evidence contract."""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Protocol


class WorkerRegistryError(ValueError):
    pass


class DuplicateWorkerError(WorkerRegistryError):
    """An active worker already owns the stable resume/idempotency key."""


class WorkerStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    INTERRUPTED = "interrupted"


ACTIVE_WORKER_STATUSES = frozenset((WorkerStatus.QUEUED, WorkerStatus.RUNNING))


def _mapping(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise WorkerRegistryError(f"{name} must be a mapping")
    if any(not isinstance(key, str) or not key.strip() for key in value):
        raise WorkerRegistryError(f"{name} keys must be non-empty text")
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _text(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkerRegistryError(f"{name} must be non-empty text")
    return value.strip()


@dataclass(frozen=True, slots=True)
class WorkerLaunch:
    worker_id: str
    parent_id: str
    role: str
    model: str
    backend: str
    effort: str
    scope: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    budgets: Mapping[str, Any]
    retry_policy: Mapping[str, Any]
    resume_key: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for name in ("worker_id", "parent_id", "role", "model", "backend", "effort", "resume_key", "idempotency_key"):
            object.__setattr__(self, name, _text(getattr(self, name), name))
            if len(getattr(self, name)) > 256:
                raise WorkerRegistryError(f"{name} exceeds its bound")
        for name in ("scope", "allowed_tools"):
            values = tuple(getattr(self, name))
            if any(not isinstance(value, str) or not value.strip() for value in values):
                raise WorkerRegistryError(f"{name} values must be non-empty text")
            if any(len(value) > 512 for value in values):
                raise WorkerRegistryError(f"{name} value exceeds its bound")
            object.__setattr__(self, name, tuple(sorted(set(values))))
        object.__setattr__(self, "budgets", _mapping(self.budgets, "budgets"))
        object.__setattr__(self, "retry_policy", _mapping(self.retry_policy, "retry_policy"))
        max_attempts = self.retry_policy.get("max_attempts", 1)
        if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or not 1 <= max_attempts <= 16:
            raise WorkerRegistryError("retry_policy.max_attempts must be between 1 and 16")


@dataclass(frozen=True, slots=True)
class WorkerRecord:
    launch: WorkerLaunch
    status: WorkerStatus
    progress: Mapping[str, Any] = MappingProxyType({})
    terminal_verification: Mapping[str, Any] = MappingProxyType({})
    error: str = ""
    revision: int = 0
    attempt_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "progress", _mapping(self.progress, "progress"))
        object.__setattr__(self, "terminal_verification", _mapping(self.terminal_verification, "terminal_verification"))
        if isinstance(self.attempt_count, bool) or not isinstance(self.attempt_count, int) or self.attempt_count < 0:
            raise WorkerRegistryError("attempt_count must be non-negative")


class WorkerRegistry(Protocol):
    def admit(self, launch: WorkerLaunch) -> WorkerRecord: ...
    def start(self, worker_id: str, *, expected_revision: int) -> WorkerRecord | None: ...
    def progress(self, worker_id: str, progress: Mapping[str, Any], *, expected_revision: int) -> WorkerRecord | None: ...
    def finish(self, worker_id: str, *, status: WorkerStatus, verification: Mapping[str, Any], error: str = "", expected_revision: int) -> WorkerRecord | None: ...
    def get(self, worker_id: str) -> WorkerRecord | None: ...


__all__ = ["ACTIVE_WORKER_STATUSES", "DuplicateWorkerError", "WorkerLaunch", "WorkerRecord", "WorkerRegistry", "WorkerRegistryError", "WorkerStatus"]
