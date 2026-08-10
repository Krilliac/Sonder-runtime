"""Self-modification domain models (SPEC-5 §18).

Pure state machine for guarded self-modification runs.
No I/O, no filesystem, no subprocess.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class SelfmodPhase(Enum):
    OBSERVED = "observed"
    PROPOSED = "proposed"
    BACKED_UP = "backed_up"
    EDITING = "editing"
    TESTING = "testing"
    REVIEWING = "reviewing"
    APPROVED = "approved"
    DEPLOYED = "deployed"
    REJECTED = "rejected"
    RESTORED = "restored"
    ROLLBACK_REQUESTED = "rollback_requested"


TERMINAL_PHASES = frozenset({
    SelfmodPhase.DEPLOYED,
    SelfmodPhase.RESTORED,
})

GUARDED_TRANSITIONS: dict[SelfmodPhase, frozenset[SelfmodPhase]] = {
    SelfmodPhase.OBSERVED: frozenset({SelfmodPhase.PROPOSED}),
    SelfmodPhase.PROPOSED: frozenset({SelfmodPhase.BACKED_UP, SelfmodPhase.REJECTED}),
    SelfmodPhase.BACKED_UP: frozenset({SelfmodPhase.EDITING, SelfmodPhase.REJECTED}),
    SelfmodPhase.EDITING: frozenset({SelfmodPhase.TESTING, SelfmodPhase.REJECTED}),
    SelfmodPhase.TESTING: frozenset({SelfmodPhase.REVIEWING, SelfmodPhase.REJECTED}),
    SelfmodPhase.REVIEWING: frozenset({SelfmodPhase.APPROVED, SelfmodPhase.REJECTED}),
    SelfmodPhase.APPROVED: frozenset({SelfmodPhase.DEPLOYED}),
    SelfmodPhase.DEPLOYED: frozenset({SelfmodPhase.ROLLBACK_REQUESTED}),
    SelfmodPhase.REJECTED: frozenset({SelfmodPhase.RESTORED}),
    SelfmodPhase.RESTORED: frozenset(),
    SelfmodPhase.ROLLBACK_REQUESTED: frozenset({SelfmodPhase.RESTORED}),
}


class InvalidPhaseTransition(Exception):
    pass


@dataclass
class SelfmodRun:
    id: str
    phase: SelfmodPhase
    objective: str
    backup_hash: str = ""
    candidate_hash: str = ""
    test_passed: bool = False
    approval_given: bool = False
    deployed_hash: str = ""
    events: list[dict] = field(default_factory=list)

    def transition(self, new_phase: SelfmodPhase) -> None:
        allowed = GUARDED_TRANSITIONS.get(self.phase, frozenset())
        if new_phase not in allowed:
            raise InvalidPhaseTransition(
                f"cannot transition from {self.phase.value} to {new_phase.value}"
            )
        self.phase = new_phase
        self.events.append({
            "from": self.phase.value,
            "to": new_phase.value,
        })

    @property
    def is_terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    @property
    def can_deploy(self) -> bool:
        return (
            self.phase == SelfmodPhase.APPROVED
            and self.test_passed
            and self.approval_given
            and bool(self.backup_hash)
        )


@dataclass(frozen=True)
class Snapshot:
    run_id: str
    file_hashes: dict[str, str] = field(default_factory=dict)
    created_at: str = ""
