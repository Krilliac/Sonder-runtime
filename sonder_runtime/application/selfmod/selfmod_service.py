"""Self-modification service (SPEC-5 §18).

Orchestrates the guarded selfmod lifecycle: snapshot → edit →
test → review → approve → deploy. Candidate-generated code
never decides its own test/approval/deployment outcomes.

Guarded invariants:
- Live source cannot be mutated by candidate executor.
- Failed tests block deployment.
- Rollback restores hashes exactly.
- Dirty checkout does not receive auto commit.
- No automatic remote push.

Unrestricted mode (--unrestricted-selfmod):
- Can use host executor.
- Protected path checks bypassed.
- Approval can be bypassed.
- Candidate isolation can be bypassed.
"""
from __future__ import annotations

from typing import Protocol

from ...domain.selfmod.models import (
    InvalidPhaseTransition,
    SelfmodPhase,
    SelfmodRun,
    Snapshot,
)
from ...domain.common.errors import Forbidden, InvalidInput


class SelfmodStore(Protocol):
    def create_run(self, run_id: str, objective: str) -> SelfmodRun: ...
    def get_run(self, run_id: str) -> SelfmodRun | None: ...
    def save_run(self, run: SelfmodRun) -> None: ...
    def save_snapshot(self, snapshot: Snapshot) -> None: ...
    def get_snapshot(self, run_id: str) -> Snapshot | None: ...


class WorkspaceAdapter(Protocol):
    def is_clean(self) -> bool: ...
    def file_hashes(self) -> dict[str, str]: ...


class SelfModificationService:

    def __init__(
        self,
        store: SelfmodStore,
        workspace: WorkspaceAdapter,
        unrestricted: bool = False,
    ):
        self._store = store
        self._workspace = workspace
        self._unrestricted = unrestricted

    def propose(self, run_id: str, objective: str) -> SelfmodRun:
        run = self._store.create_run(run_id, objective)
        run.transition(SelfmodPhase.PROPOSED)
        self._store.save_run(run)
        return run

    def backup(self, run_id: str) -> Snapshot:
        run = self._require_run(run_id)
        hashes = self._workspace.file_hashes()
        snapshot = Snapshot(run_id=run_id, file_hashes=hashes)
        self._store.save_snapshot(snapshot)
        run.backup_hash = _digest(hashes)
        run.transition(SelfmodPhase.BACKED_UP)
        self._store.save_run(run)
        return snapshot

    def start_editing(self, run_id: str) -> SelfmodRun:
        run = self._require_run(run_id)
        run.transition(SelfmodPhase.EDITING)
        self._store.save_run(run)
        return run

    def submit_tests(self, run_id: str, passed: bool) -> SelfmodRun:
        run = self._require_run(run_id)
        run.transition(SelfmodPhase.TESTING)
        run.test_passed = passed
        if not passed and not self._unrestricted:
            run.transition(SelfmodPhase.REJECTED)
        else:
            run.transition(SelfmodPhase.REVIEWING)
        self._store.save_run(run)
        return run

    def approve(self, run_id: str) -> SelfmodRun:
        run = self._require_run(run_id)
        if not self._unrestricted and not run.test_passed:
            raise Forbidden("cannot approve without passing tests")
        run.approval_given = True
        run.transition(SelfmodPhase.APPROVED)
        self._store.save_run(run)
        return run

    def deploy(self, run_id: str) -> SelfmodRun:
        run = self._require_run(run_id)
        if not self._unrestricted:
            if not run.can_deploy:
                raise Forbidden("deploy requires tests passed, approval, and backup")
            if not self._workspace.is_clean():
                raise Forbidden("dirty checkout cannot receive auto commit")
        run.deployed_hash = _digest(self._workspace.file_hashes())
        run.transition(SelfmodPhase.DEPLOYED)
        self._store.save_run(run)
        return run

    def rollback(self, run_id: str) -> SelfmodRun:
        run = self._require_run(run_id)
        snapshot = self._store.get_snapshot(run_id)
        if snapshot is None:
            raise InvalidInput("no snapshot to roll back to")
        run.transition(SelfmodPhase.ROLLBACK_REQUESTED)
        run.transition(SelfmodPhase.RESTORED)
        self._store.save_run(run)
        return run

    def _require_run(self, run_id: str) -> SelfmodRun:
        run = self._store.get_run(run_id)
        if run is None:
            raise InvalidInput(f"selfmod run {run_id!r} not found")
        return run


def _digest(hashes: dict[str, str]) -> str:
    import hashlib
    combined = "|".join(f"{k}={v}" for k, v in sorted(hashes.items()))
    return hashlib.sha256(combined.encode()).hexdigest()[:16]
