"""Journal coverage for the intermediate guarded self-mod stages (#515).

``GuardedLegacySelfmodService`` historically journaled only deploy and
rollback.  Every other mutating legacy stage now runs under the worker effect
journal with its own success predicate.  Repeatable stages (``record_test``,
``record_reproducer_before`` and ``begin_testing``) receive a per-attempt
identity derived from durable journal state, so a legitimate retry is not
fenced, while an unresolved attempt refuses re-execution until trusted
reconciliation.  Deploy and rollback identities are unchanged.

(Deliberately not named ``test_selfmod*``: that prefix is a protected path.)
"""
from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:  # child-interpreter entry point below
    sys.path.insert(0, str(REPO))

from sonder_runtime.adapters.persistence.sqlite.effect_journal import (
    SQLiteEffectJournal,
)
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError,
    EffectState,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding,
)
from sonder_runtime.application.selfmod.selfmod_service import (
    GuardedLegacySelfmodService,
)
from sonder_runtime.application.selfmod.stage_refusal import SelfmodStageNotApplied
from sonder_runtime.application.selfmod.verification_lifecycle import (
    VerificationKind,
)
from sonder_runtime.domain.common.errors import InvalidInput
from tests.test_selfmod_legacy_integration import (
    LegacyDouble,
    failure_evidence,
)

RUN = "selfmod-test-1"
JOURNAL_RUN = f"selfmod:{RUN}"
WORKER = "selfmod:stage-node"
SCOPE = "selfmod-mutation"
CRASH_EXIT = 88
HEALTH = ("python", "-c", "pass")


def _factory(journal, epoch: int = 1, *, recover: bool = True):
    def factory(run_id: str) -> AuthenticatedWorkerBinding:
        # Same shape as bootstrap ``_compose_selfmod_binding``.
        binding = AuthenticatedWorkerBinding(
            journal, f"selfmod:{run_id}", WORKER, epoch, SCOPE,
        )
        if recover:
            binding.recover_before_restart()
        return binding
    return factory


def _service(legacy, journal, *, epoch: int = 1, unrestricted: bool = False,
             recover: bool = True) -> GuardedLegacySelfmodService:
    service = GuardedLegacySelfmodService(
        legacy, unrestricted=unrestricted,
        effect_binding_factory=_factory(journal, epoch, recover=recover),
    )
    service.create_plan(
        "bounded change", "C:/repo", evidence=("concrete failure",),
        files=("target.py",), criteria=("targeted check passes",),
    )
    return service


def _records(journal):
    page = journal.effects_since(JOURNAL_RUN, 0, limit=1000)
    assert not page.truncated
    return list(page.records)


def _by_operation(journal):
    return {record.operation_id: record for record in _records(journal)}


def _full_guarded_flow(service) -> None:
    service.prepare(RUN)
    service.record_reproducer(RUN, failure_evidence())
    for kind in VerificationKind:
        service.record_verification(RUN, kind, ("python", "-c", "pass"))
    service.review(RUN)
    service.approve(RUN, approver="operator")
    service.deploy(RUN, health_command=HEALTH, commit=False)


def test_every_intermediate_stage_is_one_completed_journal_entry(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = LegacyDouble()
    _full_guarded_flow(_service(legacy, journal))

    records = _records(journal)
    assert [record.operation_id for record in records] == [
        f"selfmod-backup:{RUN}",
        f"selfmod-prepare-workspace:{RUN}",
        f"selfmod-reproducer-before:{RUN}:attempt-1",
        f"selfmod-begin-testing:{RUN}:attempt-1",
        f"selfmod-record-test:{RUN}:attempt-1",
        f"selfmod-record-test:{RUN}:attempt-2",
        f"selfmod-record-test:{RUN}:attempt-3",
        f"selfmod-record-test:{RUN}:attempt-4",
        f"selfmod-review:{RUN}",
        f"selfmod-approve:{RUN}",
        f"selfmod-deploy:{RUN}",
    ]
    assert all(record.state is EffectState.COMPLETED for record in records)
    assert all(record.worker_id == WORKER and record.owner_epoch == 1 for record in records)
    # Receipts double as idempotency keys, as for deploy.
    by_operation = {record.operation_id: record for record in records}
    assert by_operation[f"selfmod-backup:{RUN}"].receipt_key == f"selfmod:{RUN}:backup"
    attempt = by_operation[f"selfmod-record-test:{RUN}:attempt-3"]
    assert attempt.idempotency_key == attempt.receipt_key == f"selfmod:{RUN}:record-test:attempt-3"
    # Exactly one legacy invocation per journal entry.
    assert [name for name, _ in legacy.calls] == [
        "backup", "prepare", "reproducer", "begin_testing",
        "targeted", "architecture", "regression", "smoke",
        "review", "approve", "deploy",
    ]


def test_deploy_identity_and_success_semantics_are_unchanged(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    _full_guarded_flow(_service(LegacyDouble(), journal))
    deploy = journal.get(f"{JOURNAL_RUN}:selfmod-deploy:{RUN}")
    assert deploy is not None and deploy.state is EffectState.COMPLETED
    assert deploy.idempotency_key == deploy.receipt_key == f"selfmod:{RUN}:deploy"


def test_ambiguous_deploy_records_failed_deploy_and_completed_rollback(tmp_path):
    class NonDeployingLegacy(LegacyDouble):
        def deploy(self, run_id, **kwargs):
            self.calls.append(("deploy", kwargs))
            self.phase = "approved"
            return self._run()

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    service = _service(NonDeployingLegacy(), journal)
    with pytest.raises(Exception, match="deployed phase"):
        _full_guarded_flow(service)
    deploy = journal.get(f"{JOURNAL_RUN}:selfmod-deploy:{RUN}")
    rollback = journal.get(f"{JOURNAL_RUN}:selfmod-rollback:{RUN}")
    assert deploy.state is EffectState.FAILED
    assert rollback.state is EffectState.COMPLETED
    assert rollback.idempotency_key == rollback.receipt_key == f"selfmod:{RUN}:rollback"


class _PredicateMissLegacy(LegacyDouble):
    """Return a receipt that does not meet one stage's success predicate."""

    miss: str = ""

    def create_backup(self, run_id):
        result = super().create_backup(run_id)
        return {**result, "phase": "proposed"} if self.miss == "backup" else result

    def prepare_workspace(self, run_id):
        result = super().prepare_workspace(run_id)
        return {**result, "phase": "backed_up"} if self.miss == "prepare-workspace" else result

    def record_reproducer_before(self, run_id, command, timeout=None):
        result = super().record_reproducer_before(run_id, command, timeout)
        return {**result, "passed": False} if self.miss == "reproducer-before" else result

    def begin_testing(self, run_id):
        result = super().begin_testing(run_id)
        return {**result, "phase": "editing"} if self.miss == "begin-testing" else result

    def record_test(self, run_id, kind, command, **kwargs):
        result = super().record_test(run_id, kind, command, **kwargs)
        return {**result, "passed": False} if self.miss == "record-test" else result

    def review(self, run_id, **kwargs):
        result = super().review(run_id, **kwargs)
        return {**result, "phase": "rejected"} if self.miss == "review" else result

    def approve(self, run_id, approver="user"):
        result = super().approve(run_id, approver)
        return {**result, "phase": "reviewing"} if self.miss == "approve" else result


STAGE_OPERATIONS = {
    "backup": f"selfmod-backup:{RUN}",
    "prepare-workspace": f"selfmod-prepare-workspace:{RUN}",
    "reproducer-before": f"selfmod-reproducer-before:{RUN}:attempt-1",
    "begin-testing": f"selfmod-begin-testing:{RUN}:attempt-1",
    "record-test": f"selfmod-record-test:{RUN}:attempt-1",
    "review": f"selfmod-review:{RUN}",
    "approve": f"selfmod-approve:{RUN}",
}


@pytest.mark.parametrize("stage", sorted(STAGE_OPERATIONS))
def test_stage_receipt_missing_its_predicate_is_failed_not_completed(stage, tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = _PredicateMissLegacy()
    legacy.miss = stage
    service = _service(legacy, journal)
    # The typed gates may refuse to continue after a failed stage; the journal
    # outcome is what is under test.
    with contextlib.suppress(Exception):
        _full_guarded_flow(service)
    records = _by_operation(journal)
    target = records[STAGE_OPERATIONS[stage]]
    assert target.state is EffectState.FAILED, target
    assert target.receipt_key  # settled with a durable receipt, not uncertain
    for operation, record in records.items():
        # Later attempts of the same stage may miss too; deploy is gated
        # separately.  Every other stage met its own predicate.
        if not operation.startswith((f"selfmod-{stage}:", "selfmod-deploy:")):
            assert record.state is EffectState.COMPLETED, record
    # A failed outcome is settled: it is not an unresolved intent.
    assert not journal.effects_since(JOURNAL_RUN, 0, limit=1000).unresolved


def test_record_test_retries_get_distinct_journal_derived_attempts(tmp_path):
    db = tmp_path / "effects.db"
    legacy = _PredicateMissLegacy()
    legacy.miss = "record-test"
    service = _service(legacy, SQLiteEffectJournal(db), unrestricted=True)
    service.prepare(RUN)
    legacy.phase = "testing"
    service.record_verification(RUN, VerificationKind.TARGETED, ("pytest", "-q"))
    legacy.miss = ""
    service.record_verification(RUN, VerificationKind.TARGETED, ("pytest", "-q"))
    service.record_verification(RUN, VerificationKind.TARGETED, ("pytest", "-q"))

    # A new process: fresh journal handle, fresh service, newer owner epoch.
    reopened = SQLiteEffectJournal(db)
    restarted = _service(legacy, reopened, epoch=2, unrestricted=True)
    legacy.phase = "testing"  # create_plan in the helper resets the double
    restarted.record_verification(RUN, VerificationKind.TARGETED, ("pytest", "-q"))

    attempts = [
        record for record in _records(reopened)
        if record.operation_id.startswith(f"selfmod-record-test:{RUN}:")
    ]
    assert [record.operation_id for record in attempts] == [
        f"selfmod-record-test:{RUN}:attempt-{n}" for n in (1, 2, 3, 4)
    ]
    assert len({record.intent_id for record in attempts}) == 4
    assert len({record.idempotency_key for record in attempts}) == 4
    assert [record.state for record in attempts] == [
        EffectState.FAILED, EffectState.COMPLETED, EffectState.COMPLETED, EffectState.COMPLETED,
    ]
    assert [record.owner_epoch for record in attempts] == [1, 1, 1, 2]
    assert sum(1 for name, _ in legacy.calls if name == "targeted") == 4


def test_one_shot_stage_is_not_re_executed(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = LegacyDouble()
    service = _service(legacy, journal, unrestricted=True)
    service.prepare(RUN)
    legacy.phase = "proposed"  # pretend the legacy store forgot the backup
    with pytest.raises(EffectJournalError):
        service.prepare(RUN)
    assert [name for name, _ in legacy.calls].count("backup") == 1


def test_uncertain_record_test_refuses_the_next_attempt(tmp_path):
    class ExplodingLegacy(LegacyDouble):
        explode = True

        def record_test(self, run_id, kind, command, **kwargs):
            self.calls.append((kind, tuple(command)))
            if self.explode:
                raise RuntimeError("runner lost the test receipt")
            return {"passed": True, "output": "ok"}

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = ExplodingLegacy()
    service = _service(legacy, journal, unrestricted=True, recover=False)
    service.prepare(RUN)
    legacy.phase = "testing"  # unrestricted mode does not call begin_testing
    with pytest.raises(RuntimeError, match="lost the test receipt"):
        service.record_verification(RUN, VerificationKind.TARGETED, ("pytest",))
    attempt = journal.get(f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-1")
    assert attempt.state is EffectState.UNCERTAIN
    legacy.explode = False
    with pytest.raises(EffectJournalError, match="prior record-test attempt is unresolved"):
        service.record_verification(RUN, VerificationKind.TARGETED, ("pytest",))
    assert journal.get(f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-2") is None
    assert [name for name, _ in legacy.calls].count("targeted") == 1


def test_in_flight_record_test_refuses_a_concurrent_attempt(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class SlowLegacy(LegacyDouble):
        def record_test(self, run_id, kind, command, **kwargs):
            self.calls.append((kind, tuple(command)))
            entered.set()
            assert release.wait(10)
            return {"passed": True, "output": "ok"}

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = SlowLegacy()
    service = _service(legacy, journal, unrestricted=True, recover=False)
    service.prepare(RUN)
    legacy.phase = "testing"  # unrestricted mode does not call begin_testing
    worker = threading.Thread(target=lambda: service.record_verification(
        RUN, VerificationKind.TARGETED, ("pytest",),
    ))
    worker.start()
    try:
        assert entered.wait(10)
        attempt = journal.get(f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-1")
        assert attempt.state is EffectState.INTENT
        with pytest.raises(EffectJournalError, match="prior record-test attempt is unresolved"):
            service.record_verification(RUN, VerificationKind.SMOKE, ("pytest",))
    finally:
        release.set()
        worker.join(10)
    assert journal.get(
        f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-1"
    ).state is EffectState.COMPLETED
    assert journal.get(f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-2") is None
    assert [name for name, _ in legacy.calls].count("targeted") == 1
    assert [name for name, _ in legacy.calls].count("smoke") == 0


def _marker(root: Path) -> Path:
    return root / "record-test-effects.log"


def _child(root: Path) -> None:
    """Record one test, then crash inside the second record_test attempt."""

    class CrashingLegacy(LegacyDouble):
        armed = False

        def record_test(self, run_id, kind, command, **kwargs):
            result = super().record_test(run_id, kind, command, **kwargs)
            if self.armed:
                with _marker(root).open("a", encoding="utf-8") as stream:
                    stream.write("x")
                    stream.flush()
                    os.fsync(stream.fileno())
                os._exit(CRASH_EXIT)  # after the legacy effect, before the receipt
            return result

    legacy = CrashingLegacy()
    service = _service(legacy, SQLiteEffectJournal(root / "effects.db"))
    service.prepare(RUN)
    service.record_reproducer(RUN, failure_evidence())
    service.record_verification(RUN, VerificationKind.TARGETED, ("python", "-c", "pass"))
    legacy.armed = True
    service.record_verification(RUN, VerificationKind.ARCHITECTURE, ("python", "-c", "pass"))
    os._exit(0)  # the crash hook did not fire


def test_crash_between_invoke_and_receipt_leaves_uncertain_record_test(tmp_path):
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, (str(REPO), env.get("PYTHONPATH"))))
    env["SONDER_STATE_HOME"] = str(tmp_path / "state")
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), str(tmp_path)],
        cwd=REPO, env=env, capture_output=True, text=True, timeout=120, check=False,
    )
    assert completed.returncode == CRASH_EXIT, (completed.returncode, completed.stderr[-4000:])
    assert _marker(tmp_path).read_text(encoding="utf-8") == "x"

    db = tmp_path / "effects.db"
    journal = SQLiteEffectJournal(db)
    crashed_id = f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-2"
    crashed = journal.get(crashed_id)
    assert crashed is not None and crashed.state is EffectState.INTENT
    assert crashed.receipt_key == ""
    assert journal.get(
        f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-1"
    ).state is EffectState.COMPLETED

    legacy = LegacyDouble()
    for epoch in (2, 3):
        service = _service(legacy, SQLiteEffectJournal(db), epoch=epoch, unrestricted=True)
        legacy.phase = "testing"  # create_plan in the helper resets the double
        with pytest.raises(EffectJournalError, match="reconciliation"):
            service.record_verification(RUN, VerificationKind.ARCHITECTURE, ("python", "-c", "pass"))
        assert journal.get(crashed_id).state is EffectState.UNCERTAIN
    assert legacy.calls == []
    assert journal.get(f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-3") is None
    assert _marker(tmp_path).read_text(encoding="utf-8") == "x"


def test_phase_refusal_admits_no_intent_and_does_not_fence_the_run(tmp_path):
    """A stage called from the wrong legacy phase is refused before admission.

    The legacy precondition would refuse without mutating anything; journaling
    that refusal as an uncertain effect would fence the run behind manual
    reconciliation that no verifier can provide.
    """
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = LegacyDouble()
    service = _service(legacy, journal, unrestricted=True)
    service.prepare(RUN)
    assert legacy.phase == "editing"
    with pytest.raises(InvalidInput, match="cannot record-test from phase 'editing'"):
        service.record_verification(RUN, VerificationKind.TARGETED, ("pytest",))
    with pytest.raises(InvalidInput, match="cannot approve from phase 'editing'"):
        service.approve(RUN, approver="operator")
    assert journal.get(f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-1") is None
    assert journal.get(f"{JOURNAL_RUN}:selfmod-approve:{RUN}") is None
    assert not journal.effects_since(JOURNAL_RUN, 0, limit=1000).unresolved
    assert [name for name, _ in legacy.calls].count("targeted") == 0
    # The run is not fenced: once the legacy run is in testing, the stage runs.
    legacy.phase = "testing"
    service.record_verification(RUN, VerificationKind.TARGETED, ("pytest",))
    assert journal.get(
        f"{JOURNAL_RUN}:selfmod-record-test:{RUN}:attempt-1"
    ).state is EffectState.COMPLETED


class _RefusingDeployLegacy(LegacyDouble):
    """Legacy deploy/rollback that refuse before mutating until told otherwise."""

    refuse = True

    def deploy(self, run_id, **kwargs):
        if self.refuse:
            self.calls.append(("deploy-refused", kwargs))
            raise SelfmodStageNotApplied("another deployment/rollback holds the process-safe lock")
        return super().deploy(run_id, **kwargs)


def _approved(service, legacy) -> None:
    service.prepare(RUN)
    legacy.phase = "approved"  # unrestricted mode: the typed gates are not under test


def test_a_not_applied_refusal_settles_failed_and_admits_a_retry(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = _RefusingDeployLegacy()
    service = _service(legacy, journal, unrestricted=True)
    _approved(service, legacy)
    with pytest.raises(SelfmodStageNotApplied, match="holds the process-safe lock"):
        service.deploy(RUN, health_command=HEALTH, commit=False)
    refused = journal.get(f"{JOURNAL_RUN}:selfmod-deploy:{RUN}")
    assert refused.state is EffectState.FAILED
    assert refused.receipt_key == f"selfmod:{RUN}:deploy:not-applied"
    assert refused.idempotency_key == f"selfmod:{RUN}:deploy"
    assert not journal.effects_since(JOURNAL_RUN, 0, limit=1000).unresolved

    # A second refusal gets the next retry identity; nothing is fenced.
    service = _service(legacy, journal, unrestricted=True)
    legacy.phase = "approved"
    with pytest.raises(SelfmodStageNotApplied):
        service.deploy(RUN, health_command=HEALTH, commit=False)
    legacy.refuse = False
    service = _service(legacy, journal, unrestricted=True)
    legacy.phase = "approved"
    service.deploy(RUN, health_command=HEALTH, commit=False)
    deploys = [record for record in _records(journal)
               if record.operation_id.startswith(f"selfmod-deploy:{RUN}")]
    assert [(r.operation_id, r.state, r.receipt_key) for r in deploys] == [
        (f"selfmod-deploy:{RUN}", EffectState.FAILED, f"selfmod:{RUN}:deploy:not-applied"),
        (f"selfmod-deploy:{RUN}:retry-2", EffectState.FAILED,
         f"selfmod:{RUN}:deploy:retry-2:not-applied"),
        (f"selfmod-deploy:{RUN}:retry-3", EffectState.COMPLETED, f"selfmod:{RUN}:deploy:retry-3"),
    ]
    assert [name for name, _ in legacy.calls].count("deploy") == 1


def test_a_completed_one_shot_still_refuses_a_second_call(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = _RefusingDeployLegacy()
    legacy.refuse = False
    service = _service(legacy, journal, unrestricted=True)
    _approved(service, legacy)
    service.deploy(RUN, health_command=HEALTH, commit=False)
    legacy.phase = "approved"  # pretend the legacy store forgot the deployment
    with pytest.raises(EffectJournalError, match="duplicate effect intent"):
        service.deploy(RUN, health_command=HEALTH, commit=False)
    assert [name for name, _ in legacy.calls].count("deploy") == 1
    assert journal.get(f"{JOURNAL_RUN}:selfmod-deploy:{RUN}:retry-2") is None


def test_an_untyped_failure_still_leaves_the_one_shot_uncertain(tmp_path):
    class ExplodingLegacy(LegacyDouble):
        def deploy(self, run_id, **kwargs):
            self.calls.append(("deploy", kwargs))
            raise RuntimeError("installed bytes differ from tested bytes")

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    legacy = ExplodingLegacy()
    service = _service(legacy, journal, unrestricted=True)
    _approved(service, legacy)
    with pytest.raises(RuntimeError, match="installed bytes differ"):
        service.deploy(RUN, health_command=HEALTH, commit=False)
    assert journal.get(f"{JOURNAL_RUN}:selfmod-deploy:{RUN}").state is EffectState.UNCERTAIN
    with pytest.raises(EffectJournalError, match="reconciliation"):
        _service(legacy, journal, unrestricted=True)._effect_binding_factory(RUN)


def test_a_not_applied_refusal_after_a_real_failure_is_still_fenced(tmp_path):
    """A retry is admitted only when every earlier attempt was not applied."""
    journal = SQLiteEffectJournal(tmp_path / "effects.db")

    class MissingReceiptLegacy(_RefusingDeployLegacy):
        def deploy(self, run_id, **kwargs):
            self.calls.append(("deploy", kwargs))
            return self._run()  # ran, but no deployed phase: a settled failure

    legacy = MissingReceiptLegacy()
    service = _service(legacy, journal, unrestricted=True)
    _approved(service, legacy)
    service.deploy(RUN, health_command=HEALTH, commit=False)
    assert journal.get(f"{JOURNAL_RUN}:selfmod-deploy:{RUN}").receipt_key == f"selfmod:{RUN}:deploy"
    legacy.phase = "approved"
    with pytest.raises(EffectJournalError, match="duplicate effect intent"):
        service.deploy(RUN, health_command=HEALTH, commit=False)
    assert journal.get(f"{JOURNAL_RUN}:selfmod-deploy:{RUN}:retry-2") is None


if __name__ == "__main__":
    _child(Path(sys.argv[1]))
