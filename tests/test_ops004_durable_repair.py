from __future__ import annotations

import pytest

from sonder_runtime.adapters.persistence.repair_journal import RepairJournal
from sonder_runtime.application.operations.durable_repair import (
    DurableRepairExecutor,
    RepairConflict,
    RepairRecoveryRequired,
)
from sonder_runtime.application.operations.startup_reconciliation import (
    DrainAction,
    RecordKind,
    ReconciliationClass,
    ReconciliationResult,
    StartupObservation,
    reconcile_observation,
)


def _result(status="queued"):
    return reconcile_observation(StartupObservation(RecordKind.JOB, "job-1", status))


def test_repair_is_durable_and_replay_is_idempotent(tmp_path):
    journal = RepairJournal(tmp_path / "repairs.json")
    executor = DurableRepairExecutor(journal)
    calls = []
    first = executor.execute("repair-1", _result(), lambda result: calls.append(result.observation.record_id) or {"accepted": True})
    second = executor.execute("repair-1", _result(), lambda _result: calls.append("duplicate"))
    assert first.status == "applied"
    assert second.replayed is True
    assert second.value == {"accepted": True}
    assert calls == ["job-1"]
    assert DurableRepairExecutor(RepairJournal(tmp_path / "repairs.json")).execute("repair-1", _result(), lambda _: "bad").replayed


def test_repair_key_conflict_and_pending_state_fail_closed(tmp_path):
    journal = RepairJournal(tmp_path / "repairs.json")
    executor = DurableRepairExecutor(journal)
    executor.execute("repair-1", _result(), lambda _: "ok")
    with pytest.raises(RepairConflict):
        executor.execute("repair-1", _result("interrupted"), lambda _: "wrong")
    journal.replace("repair-1", {"record_id": "job-1", "action": "resume", "fingerprint": journal.get("repair-1")["fingerprint"], "status": "pending"})
    with pytest.raises(RepairRecoveryRequired):
        executor.execute("repair-1", _result(), lambda _: "unsafe")


def test_failed_effect_leaves_pending_record_for_reconciliation(tmp_path):
    executor = DurableRepairExecutor(RepairJournal(tmp_path / "repairs.json"))
    with pytest.raises(RuntimeError, match="platform unavailable"):
        executor.execute("repair-failed", _result(), lambda _: (_ for _ in ()).throw(RuntimeError("platform unavailable")))
    with pytest.raises(RepairRecoveryRequired):
        executor.execute("repair-failed", _result(), lambda _: "must not retry")


def test_unsafe_cleanup_and_mismatched_classification_never_run_effect(tmp_path):
    executor = DurableRepairExecutor(RepairJournal(tmp_path / "repairs.json"))
    called = []
    orphan = reconcile_observation(StartupObservation(RecordKind.SUBAGENT, "child", "running", owner_instance_id="old", owner_alive=False, process_id=42))
    assert orphan.action is DrainAction.CLEANUP_PROCESS_TREE
    with pytest.raises(RepairConflict):
        executor.execute("repair-unsafe", orphan, lambda _: called.append(True))
    mismatch = ReconciliationResult(orphan.observation, ReconciliationClass.RESUMABLE, True, DrainAction.MARK_INTERRUPTED, "bad")
    with pytest.raises(RepairConflict):
        executor.execute("repair-mismatch", mismatch, lambda _: called.append(True))
    assert called == []
