"""Automation domain models (SPEC-5 §11).

Pure domain types — no I/O.  State machine for automation runs with
CAS-based claim/lease protocol.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class RunKind(Enum):
    TASK = "task"
    AUTOPILOT = "autopilot"
    FLEET = "fleet"
    WORKFLOW = "workflow"


class RunStatus(Enum):
    PENDING = "pending"
    CLAIMED = "claimed"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_STATUSES = frozenset({
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
})

VALID_TRANSITIONS: dict[RunStatus, frozenset[RunStatus]] = {
    RunStatus.PENDING: frozenset({RunStatus.CLAIMED, RunStatus.CANCELLED}),
    RunStatus.CLAIMED: frozenset({RunStatus.RUNNING, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.RUNNING: frozenset({RunStatus.PAUSED, RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.CANCELLED}),
    RunStatus.PAUSED: frozenset({RunStatus.RUNNING, RunStatus.CANCELLED}),
    RunStatus.COMPLETED: frozenset(),
    RunStatus.FAILED: frozenset(),
    RunStatus.CANCELLED: frozenset(),
}


class InvalidTransition(Exception):
    pass


@dataclass
class AutomationRun:
    id: str
    kind: RunKind
    status: RunStatus
    revision: int
    objective: str
    correlation_id: str
    config: dict = field(default_factory=dict)
    created_at: str = ""
    updated_at: str = ""

    def transition(self, new_status: RunStatus) -> None:
        allowed = VALID_TRANSITIONS.get(self.status, frozenset())
        if new_status not in allowed:
            raise InvalidTransition(
                f"cannot transition from {self.status.value} to {new_status.value}"
            )
        self.status = new_status
        self.revision += 1

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_STATUSES


@dataclass
class Claim:
    run_id: str
    worker_id: str
    lease_until: str
    revision: int

    def matches(self, run: AutomationRun) -> bool:
        return (
            self.run_id == run.id
            and self.revision == run.revision
        )
