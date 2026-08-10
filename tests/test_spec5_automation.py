"""SPEC-5 WP6: Automation and agents contract tests."""
from __future__ import annotations

import pytest
from datetime import datetime, timezone, timedelta

from sonder_runtime.domain.automation.models import (
    AutomationRun,
    Claim,
    InvalidTransition,
    RunKind,
    RunStatus,
    TERMINAL_STATUSES,
    VALID_TRANSITIONS,
)
from sonder_runtime.application.automation.automation_service import (
    AutomationService,
)
from sonder_runtime.domain.common.errors import Forbidden, InvalidInput


# ---------------------------------------------------------------------------
# Domain: state machine
# ---------------------------------------------------------------------------

class TestRunStatus:
    def test_terminal_statuses_complete(self):
        assert RunStatus.COMPLETED in TERMINAL_STATUSES
        assert RunStatus.FAILED in TERMINAL_STATUSES
        assert RunStatus.CANCELLED in TERMINAL_STATUSES

    def test_pending_can_become_claimed(self):
        assert RunStatus.CLAIMED in VALID_TRANSITIONS[RunStatus.PENDING]

    def test_running_can_complete(self):
        assert RunStatus.COMPLETED in VALID_TRANSITIONS[RunStatus.RUNNING]

    def test_completed_cannot_transition(self):
        assert len(VALID_TRANSITIONS[RunStatus.COMPLETED]) == 0

    def test_invalid_transition_rejected(self):
        run = AutomationRun(
            id="r1", kind=RunKind.TASK, status=RunStatus.COMPLETED,
            revision=0, objective="test", correlation_id="c1",
        )
        with pytest.raises(InvalidTransition):
            run.transition(RunStatus.RUNNING)

    def test_valid_transition_increments_revision(self):
        run = AutomationRun(
            id="r1", kind=RunKind.AUTOPILOT, status=RunStatus.PENDING,
            revision=0, objective="test", correlation_id="c1",
        )
        run.transition(RunStatus.CLAIMED)
        assert run.status == RunStatus.CLAIMED
        assert run.revision == 1

    def test_full_lifecycle(self):
        run = AutomationRun(
            id="r1", kind=RunKind.FLEET, status=RunStatus.PENDING,
            revision=0, objective="test", correlation_id="c1",
        )
        run.transition(RunStatus.CLAIMED)
        run.transition(RunStatus.RUNNING)
        run.transition(RunStatus.PAUSED)
        run.transition(RunStatus.RUNNING)
        run.transition(RunStatus.COMPLETED)
        assert run.is_terminal
        assert run.revision == 5


class TestClaim:
    def test_claim_matches_revision(self):
        run = AutomationRun(
            id="r1", kind=RunKind.TASK, status=RunStatus.PENDING,
            revision=3, objective="test", correlation_id="c1",
        )
        claim = Claim(run_id="r1", worker_id="w1", lease_until="", revision=3)
        assert claim.matches(run)

    def test_claim_mismatch_on_revision(self):
        run = AutomationRun(
            id="r1", kind=RunKind.TASK, status=RunStatus.PENDING,
            revision=4, objective="test", correlation_id="c1",
        )
        claim = Claim(run_id="r1", worker_id="w1", lease_until="", revision=3)
        assert not claim.matches(run)


# ---------------------------------------------------------------------------
# Application: AutomationService
# ---------------------------------------------------------------------------

class _FakeStore:
    def __init__(self):
        self.runs: dict[str, AutomationRun] = {}
        self.claims: dict[str, Claim] = {}

    def create_run(self, run_id, kind, objective, correlation_id, config=None):
        now = datetime.now(timezone.utc).isoformat()
        run = AutomationRun(
            id=run_id, kind=kind, status=RunStatus.PENDING,
            revision=0, objective=objective, correlation_id=correlation_id,
            config=config or {}, created_at=now, updated_at=now,
        )
        self.runs[run_id] = run
        return run

    def get_run(self, run_id):
        return self.runs.get(run_id)

    def list_runs(self, kind=None, include_terminal=False):
        return [
            r for r in self.runs.values()
            if (kind is None or r.kind == kind)
            and (include_terminal or not r.is_terminal)
        ]

    def save_run(self, run):
        self.runs[run.id] = run

    def try_claim(self, run_id, worker_id, lease_seconds, expected_revision):
        run = self.runs.get(run_id)
        if run is None or run.revision != expected_revision:
            return None
        if run_id in self.claims:
            return None
        until = (
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds)
        ).isoformat()
        claim = Claim(
            run_id=run_id, worker_id=worker_id,
            lease_until=until, revision=expected_revision,
        )
        self.claims[run_id] = claim
        return claim

    def release_claim(self, run_id, worker_id):
        if run_id in self.claims and self.claims[run_id].worker_id == worker_id:
            del self.claims[run_id]

    def get_claim(self, run_id):
        return self.claims.get(run_id)

    def reconcile_expired_claims(self, now_iso):
        expired = [
            k for k, v in self.claims.items()
            if v.lease_until < now_iso
        ]
        for k in expired:
            del self.claims[k]
        return len(expired)


class TestAutomationService:
    def test_create_run(self):
        store = _FakeStore()
        svc = AutomationService(store)
        run = svc.create("r1", RunKind.AUTOPILOT, "build something", "c1")
        assert run.status == RunStatus.PENDING
        assert run.objective == "build something"

    def test_create_empty_objective_rejected(self):
        svc = AutomationService(_FakeStore())
        with pytest.raises(InvalidInput):
            svc.create("r1", RunKind.TASK, "  ", "c1")

    def test_claim_race_one_winner(self):
        store = _FakeStore()
        svc = AutomationService(store)
        svc.create("r1", RunKind.FLEET, "test", "c1")

        claim1 = svc.claim("r1", "worker-A")
        assert claim1.worker_id == "worker-A"

        with pytest.raises(Forbidden, match="race lost"):
            svc.claim("r1", "worker-B")

    def test_expired_lease_recoverable(self):
        store = _FakeStore()
        svc = AutomationService(store)
        svc.create("r1", RunKind.AUTOPILOT, "test", "c1")
        svc.claim("r1", "worker-A", lease_seconds=1)

        future = (
            datetime.now(timezone.utc) + timedelta(hours=1)
        ).isoformat()
        recovered = svc.reconcile_expired(future)
        assert recovered == 1

    def test_invalid_transition_rejected(self):
        store = _FakeStore()
        svc = AutomationService(store)
        svc.create("r1", RunKind.TASK, "test", "c1")
        svc.claim("r1", "w1")

        svc.transition("r1", "w1", RunStatus.RUNNING)
        svc.transition("r1", "w1", RunStatus.COMPLETED)

        with pytest.raises(InvalidTransition):
            svc.transition("r1", "w1", RunStatus.RUNNING)

    def test_restart_resumes_durable_state(self):
        store = _FakeStore()
        svc = AutomationService(store)
        svc.create("r1", RunKind.AUTOPILOT, "test", "c1")
        svc.claim("r1", "w1")
        svc.transition("r1", "w1", RunStatus.RUNNING)
        svc.transition("r1", "w1", RunStatus.PAUSED)

        svc2 = AutomationService(store)
        run = store.get_run("r1")
        assert run.status == RunStatus.PAUSED
        assert run.revision == 3

    def test_terminal_releases_claim(self):
        store = _FakeStore()
        svc = AutomationService(store)
        svc.create("r1", RunKind.TASK, "test", "c1")
        svc.claim("r1", "w1")
        svc.transition("r1", "w1", RunStatus.RUNNING)
        svc.transition("r1", "w1", RunStatus.COMPLETED)
        assert store.get_claim("r1") is None

    def test_wrong_worker_forbidden(self):
        store = _FakeStore()
        svc = AutomationService(store)
        svc.create("r1", RunKind.FLEET, "test", "c1")
        svc.claim("r1", "w1")

        with pytest.raises(Forbidden, match="claimed by"):
            svc.transition("r1", "w2", RunStatus.RUNNING)
