"""SPEC-5 WP7: Self-modification domain contract tests."""
from __future__ import annotations

import pytest

from sonder_runtime.domain.selfmod.models import (
    GUARDED_TRANSITIONS,
    InvalidPhaseTransition,
    SelfmodPhase,
    SelfmodRun,
    Snapshot,
    TERMINAL_PHASES,
)
from sonder_runtime.application.selfmod.selfmod_service import (
    SelfModificationService,
)
from sonder_runtime.domain.common.errors import Forbidden, InvalidInput


# ---------------------------------------------------------------------------
# Domain: state machine
# ---------------------------------------------------------------------------

class TestSelfmodPhases:
    def test_guarded_happy_path(self):
        run = SelfmodRun(id="r1", phase=SelfmodPhase.OBSERVED, objective="test")
        run.transition(SelfmodPhase.PROPOSED)
        run.transition(SelfmodPhase.BACKED_UP)
        run.transition(SelfmodPhase.EDITING)
        run.transition(SelfmodPhase.TESTING)
        run.transition(SelfmodPhase.REVIEWING)
        run.transition(SelfmodPhase.APPROVED)
        run.transition(SelfmodPhase.DEPLOYED)
        assert run.is_terminal

    def test_rejection_path(self):
        run = SelfmodRun(id="r1", phase=SelfmodPhase.TESTING, objective="test")
        run.transition(SelfmodPhase.REJECTED)
        run.transition(SelfmodPhase.RESTORED)
        assert run.is_terminal

    def test_rollback_path(self):
        run = SelfmodRun(id="r1", phase=SelfmodPhase.DEPLOYED, objective="test")
        run.transition(SelfmodPhase.ROLLBACK_REQUESTED)
        run.transition(SelfmodPhase.RESTORED)
        assert run.is_terminal

    def test_invalid_skip(self):
        run = SelfmodRun(id="r1", phase=SelfmodPhase.OBSERVED, objective="test")
        with pytest.raises(InvalidPhaseTransition):
            run.transition(SelfmodPhase.DEPLOYED)

    def test_deployed_is_terminal(self):
        assert SelfmodPhase.DEPLOYED in TERMINAL_PHASES

    def test_restored_is_terminal(self):
        assert SelfmodPhase.RESTORED in TERMINAL_PHASES

    def test_can_deploy_requires_all(self):
        run = SelfmodRun(
            id="r1", phase=SelfmodPhase.APPROVED, objective="test",
            test_passed=True, approval_given=True, backup_hash="abc",
        )
        assert run.can_deploy

    def test_can_deploy_fails_without_tests(self):
        run = SelfmodRun(
            id="r1", phase=SelfmodPhase.APPROVED, objective="test",
            test_passed=False, approval_given=True, backup_hash="abc",
        )
        assert not run.can_deploy


# ---------------------------------------------------------------------------
# Application: SelfModificationService
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self):
        self.runs = {}
        self.snapshots = {}

    def create_run(self, run_id, objective):
        run = SelfmodRun(id=run_id, phase=SelfmodPhase.OBSERVED, objective=objective)
        self.runs[run_id] = run
        return run

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def save_run(self, run):
        self.runs[run.id] = run

    def save_snapshot(self, snapshot):
        self.snapshots[snapshot.run_id] = snapshot

    def get_snapshot(self, run_id):
        return self.snapshots.get(run_id)


class _FakeWorkspace:
    def __init__(self, clean=True, hashes=None):
        self._clean = clean
        self._hashes = hashes or {"main.py": "abc123"}

    def is_clean(self):
        return self._clean

    def file_hashes(self):
        return dict(self._hashes)


class TestGuardedSelfmod:
    def test_full_guarded_lifecycle(self):
        store = _FakeStore()
        ws = _FakeWorkspace(clean=True)
        svc = SelfModificationService(store, ws)

        run = svc.propose("r1", "add feature")
        assert run.phase == SelfmodPhase.PROPOSED

        svc.backup("r1")
        svc.start_editing("r1")
        svc.submit_tests("r1", passed=True)
        svc.approve("r1")
        result = svc.deploy("r1")
        assert result.phase == SelfmodPhase.DEPLOYED

    def test_failed_tests_block_deployment(self):
        store = _FakeStore()
        ws = _FakeWorkspace()
        svc = SelfModificationService(store, ws)

        svc.propose("r1", "test")
        svc.backup("r1")
        svc.start_editing("r1")
        run = svc.submit_tests("r1", passed=False)
        assert run.phase == SelfmodPhase.REJECTED

    def test_rollback_restores(self):
        store = _FakeStore()
        ws = _FakeWorkspace()
        svc = SelfModificationService(store, ws)

        svc.propose("r1", "test")
        svc.backup("r1")
        svc.start_editing("r1")
        svc.submit_tests("r1", passed=True)
        svc.approve("r1")
        svc.deploy("r1")
        run = svc.rollback("r1")
        assert run.phase == SelfmodPhase.RESTORED

    def test_dirty_checkout_blocks_deploy(self):
        store = _FakeStore()
        ws = _FakeWorkspace(clean=False)
        svc = SelfModificationService(store, ws)

        svc.propose("r1", "test")
        svc.backup("r1")
        svc.start_editing("r1")
        svc.submit_tests("r1", passed=True)
        svc.approve("r1")
        with pytest.raises(Forbidden, match="dirty checkout"):
            svc.deploy("r1")

    def test_snapshot_preserved(self):
        store = _FakeStore()
        ws = _FakeWorkspace(hashes={"a.py": "hash1", "b.py": "hash2"})
        svc = SelfModificationService(store, ws)
        svc.propose("r1", "test")
        snapshot = svc.backup("r1")
        assert snapshot.file_hashes == {"a.py": "hash1", "b.py": "hash2"}


class TestUnrestrictedSelfmod:
    def test_failed_tests_do_not_block(self):
        store = _FakeStore()
        ws = _FakeWorkspace()
        svc = SelfModificationService(store, ws, unrestricted=True)

        svc.propose("r1", "test")
        svc.backup("r1")
        svc.start_editing("r1")
        run = svc.submit_tests("r1", passed=False)
        assert run.phase == SelfmodPhase.REVIEWING

    def test_dirty_checkout_allowed(self):
        store = _FakeStore()
        ws = _FakeWorkspace(clean=False)
        svc = SelfModificationService(store, ws, unrestricted=True)

        svc.propose("r1", "test")
        svc.backup("r1")
        svc.start_editing("r1")
        svc.submit_tests("r1", passed=True)
        svc.approve("r1")
        run = svc.deploy("r1")
        assert run.phase == SelfmodPhase.DEPLOYED

    def test_startup_flag_required(self):
        svc = SelfModificationService(_FakeStore(), _FakeWorkspace())
        assert not svc._unrestricted
        svc2 = SelfModificationService(
            _FakeStore(), _FakeWorkspace(), unrestricted=True,
        )
        assert svc2._unrestricted
