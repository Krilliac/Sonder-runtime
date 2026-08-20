"""Restart-safe workflow recovery (WP5-WORKFLOW-001).

The service in this module is persistence-neutral.  A store adapter owns the
transaction that writes a snapshot and its idempotency ledger; this module
enforces the recovery and compare-and-set rules around that adapter.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, Protocol

from ...domain.common.errors import Conflict, InvalidInput, NotFound
from ..ports.jobs import JobStatus


ACTIVE_STATUSES = frozenset({JobStatus.PENDING, JobStatus.CLAIMED, JobStatus.RUNNING, JobStatus.PAUSED, JobStatus.INTERRUPTED})
TERMINAL_STATUSES = frozenset({JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED})


class RestartDisposition(str, Enum):
    CONTINUE = "continue"
    RESTART_REQUIRED = "restart_required"
    TERMINAL = "terminal"


@dataclass(frozen=True, slots=True)
class WorkflowSnapshot:
    """The complete durable state needed to safely resume one workflow."""

    workflow_id: str
    execution_id: str
    revision: int = 0
    next_step: int = 0
    status: JobStatus = JobStatus.PENDING
    state: Mapping[str, Any] = field(default_factory=dict)
    owner_instance_id: str = ""
    result: Any = None
    error: str = ""
    completed_resume_keys: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.workflow_id.strip() or not self.execution_id.strip():
            raise InvalidInput("workflow_id and execution_id are required")
        if self.revision < 0 or self.next_step < 0:
            raise InvalidInput("snapshot revision and next_step cannot be negative")
        if self.status in TERMINAL_STATUSES and not (self.result is not None or self.error):
            raise InvalidInput("terminal snapshots require a result or error")


@dataclass(frozen=True, slots=True)
class RestartDecision:
    disposition: RestartDisposition
    reason: str
    snapshot: WorkflowSnapshot


@dataclass(frozen=True, slots=True)
class ResumeResult:
    snapshot: WorkflowSnapshot
    value: Any
    replayed: bool = False


class RestartRecoveryStore(Protocol):
    def get(self, execution_id: str) -> WorkflowSnapshot | None: ...

    def compare_and_set(
        self,
        snapshot: WorkflowSnapshot,
        *,
        expected_revision: int,
    ) -> bool: ...


class RestartRecoveryService:
    """Detect abandoned executions and resume them exactly once per key."""

    def __init__(self, store: RestartRecoveryStore) -> None:
        self._store = store

    def snapshot(self, execution_id: str) -> WorkflowSnapshot:
        current = self._load(execution_id)
        return replace(current, state=dict(current.state), completed_resume_keys=dict(current.completed_resume_keys))

    def detect_restart(self, execution_id: str, *, instance_id: str) -> RestartDecision:
        if not isinstance(instance_id, str) or not instance_id.strip():
            raise InvalidInput("instance_id is required")
        current = self._load(execution_id)
        if current.status in TERMINAL_STATUSES:
            return RestartDecision(RestartDisposition.TERMINAL, "workflow is terminal", current)
        if current.status is JobStatus.INTERRUPTED:
            return RestartDecision(RestartDisposition.RESTART_REQUIRED, "workflow was interrupted", current)
        if current.owner_instance_id and current.owner_instance_id != instance_id:
            return RestartDecision(RestartDisposition.RESTART_REQUIRED, "workflow belongs to another instance", current)
        return RestartDecision(RestartDisposition.CONTINUE, "workflow is owned by this instance", current)

    def resume(
        self,
        execution_id: str,
        *,
        instance_id: str,
        resume_key: str,
        run: Callable[[WorkflowSnapshot], Any],
    ) -> ResumeResult:
        """Run one recovery attempt; repeated keys replay without running again."""
        if not isinstance(resume_key, str) or not resume_key.strip():
            raise InvalidInput("resume_key is required")
        decision = self.detect_restart(execution_id, instance_id=instance_id)
        current = decision.snapshot
        if decision.disposition is RestartDisposition.TERMINAL:
            raise Conflict("terminal workflow cannot resume")
        if resume_key in current.completed_resume_keys:
            return ResumeResult(current, current.completed_resume_keys[resume_key], replayed=True)

        value = run(current)
        updated = replace(
            current,
            revision=current.revision + 1,
            status=JobStatus.RUNNING,
            owner_instance_id=instance_id,
            completed_resume_keys={**current.completed_resume_keys, resume_key: value},
        )
        if not self._store.compare_and_set(updated, expected_revision=current.revision):
            raise Conflict("workflow snapshot changed during resume")
        return ResumeResult(updated, value)

    def finish(
        self,
        execution_id: str,
        *,
        instance_id: str,
        status: JobStatus,
        result: Any = None,
        error: str = "",
    ) -> WorkflowSnapshot:
        if status not in TERMINAL_STATUSES:
            raise InvalidInput("finish requires a terminal status")
        current = self._load(execution_id)
        if current.status in TERMINAL_STATUSES:
            return current
        if current.owner_instance_id and current.owner_instance_id != instance_id:
            raise Conflict("workflow is owned by another instance")
        if status is JobStatus.SUCCEEDED and result is None:
            raise InvalidInput("successful workflow requires a result")
        if status is not JobStatus.SUCCEEDED and not error.strip():
            raise InvalidInput("failed or cancelled workflow requires an error")
        updated = replace(current, revision=current.revision + 1, status=status, owner_instance_id=instance_id, result=result, error=error)
        if not self._store.compare_and_set(updated, expected_revision=current.revision):
            raise Conflict("workflow snapshot changed during finish")
        return updated

    def _load(self, execution_id: str) -> WorkflowSnapshot:
        snapshot = self._store.get(execution_id)
        if snapshot is None:
            raise NotFound(f"workflow execution {execution_id!r} not found")
        return snapshot


__all__ = [
    "RestartDecision", "RestartDisposition", "RestartRecoveryService",
    "RestartRecoveryStore", "ResumeResult", "WorkflowSnapshot",
]
