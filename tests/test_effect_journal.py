from __future__ import annotations

from dataclasses import replace
import pytest

from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import SQLiteRuntimeCheckpointRepository
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent, EffectJournalError, EffectOutcome, EffectState, JournalBinding, bound,
)
from sonder_runtime.application.ports.runtime_checkpoints import RestoreStatus, RuntimeCheckpoint


def _intent(intent_id="i-1", run_id="run-1", worker_id="w-1", key="k-1", request_digest="a" * 64):
    return EffectIntent(intent_id, run_id, worker_id, "op-1", "/workspace", 2, key, request_digest)


def _checkpoint(generation=0):
    return RuntimeCheckpoint("run-1", generation, {"purpose": "test"},
        workers={"w-1": {"status": "paused"}}, checkpoint_id=f"cp-{generation}")


def test_intent_is_idempotent_and_conflicts_are_rejected(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    first = journal.begin(_intent())
    replay = journal.begin(_intent())
    assert replay != first and replay.replayed and first.sequence == 1
    with pytest.raises(EffectJournalError):
        journal.begin(_intent(request_digest="b" * 64))
    for altered in (
        replace(_intent(), scope="/other"),
        replace(_intent(), owner_epoch=3),
        replace(_intent(), idempotency_key="different"),
        replace(_intent(), reconciliation="query"),
        replace(_intent(), intent_id="different"),
    ):
        with pytest.raises(EffectJournalError):
            journal.begin(altered)


def test_outcome_is_durable_and_replay_is_exact(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    journal.begin(_intent())
    done = journal.outcome(EffectOutcome("i-1", EffectState.COMPLETED, "b" * 64, "receipt-1", worker_id="w-1", owner_epoch=2))
    assert done.state is EffectState.COMPLETED
    assert SQLiteEffectJournal(tmp_path / "effects.db").outcome(
        EffectOutcome("i-1", EffectState.COMPLETED, "b" * 64, "receipt-1", worker_id="w-1", owner_epoch=2)
    ).receipt_key == "receipt-1"
    with pytest.raises(EffectJournalError):
        journal.outcome(EffectOutcome("i-1", EffectState.FAILED, "c" * 64, "receipt-2", worker_id="w-1", owner_epoch=2))
    with pytest.raises(EffectJournalError, match="worker identity"):
        EffectOutcome("i-1", EffectState.COMPLETED, "b" * 64, "receipt-1")
    with pytest.raises(EffectJournalError, match="owner"):
        journal.outcome(EffectOutcome("i-1", EffectState.COMPLETED, "b" * 64, "receipt-1", worker_id="stale", owner_epoch=2))


def test_recovery_reattaches_live_owner_and_marks_dead_owner_uncertain(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    journal.begin(_intent("live", worker_id="w-live", key="live"))
    journal.begin(_intent("dead", worker_id="w-dead", key="dead"))
    decision = journal.recover("run-1", live_workers={"w-live": 2})
    assert decision.action == "reattach"
    assert decision.intent_ids == ("live", "dead")
    assert journal.get("dead").state is EffectState.UNCERTAIN
    with pytest.raises(EffectJournalError):
        journal.validate_checkpoint("run-1", journal.high_water("run-1"))


def test_late_receipt_from_orphaned_owner_is_not_accepted(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    journal.begin(_intent(worker_id="w-dead"))
    journal.recover("run-1", live_workers={})
    with pytest.raises(EffectJournalError, match="late receipt"):
        journal.outcome(EffectOutcome(
            "i-1", EffectState.COMPLETED, "b" * 64, "receipt-1", worker_id="w-dead", owner_epoch=2,
        ))


def test_recovery_requires_exact_live_owner_epoch_and_complete_page(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    journal.begin(_intent("first", worker_id="worker", key="first"))
    journal.begin(_intent("second", worker_id="worker", key="second"))
    with pytest.raises(EffectJournalError, match="bounded page"):
        journal.recover("run-1", live_workers={"worker": 2}, max_records=1)
    assert journal.get("first").state is EffectState.INTENT
    decision = journal.recover("run-1", live_workers={"worker": 3})
    assert decision.action == "reconcile"
    assert journal.get("first").state is EffectState.UNCERTAIN
    assert journal.get("second").state is EffectState.UNCERTAIN


def test_checkpoint_binds_effect_high_water_and_refuses_unresolved_replay(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "state.db")
    checkpoints = SQLiteRuntimeCheckpointRepository(
        tmp_path / "state.db", seal_key=b"k" * 32, effect_journal=journal,
    )
    journal.begin(_intent())
    with pytest.raises(EffectJournalError):
        checkpoints.save(_checkpoint(), expected_generation=-1)
    journal.outcome(EffectOutcome("i-1", EffectState.COMPLETED, "b" * 64, "receipt-1", worker_id="w-1", owner_epoch=2))
    checkpoints.save(_checkpoint(), expected_generation=-1)
    assert checkpoints.restore("run-1").status is RestoreStatus.RESTORED


def test_gateway_records_intent_before_live_invocation(tmp_path):
    from sonder_runtime.application.tools.gateway_contract import (
        ApprovalMode, RedactedOutput, ToolGateway, ToolGatewayRequest, ToolInvocationOutput,
        ToolPermission, ToolScope,
    )

    class Schema:
        def validate(self, *_args): pass
    class Approval:
        def approve(self, _request): return True
    class Invoker:
        def __init__(self, journal): self.journal = journal; self.calls = 0
        def invoke(self, _request):
            self.calls += 1
            assert self.journal.high_water("run-1") == 1
            return ToolInvocationOutput(True, output="changed")
    class Redactor:
        def redact(self, _tool, value): return RedactedOutput(value, False)
    class Receipts:
        def record(self, receipt): self.receipt = receipt

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    receipts = Receipts()
    invoker = Invoker(journal)
    gateway = ToolGateway(Schema(), type("Permissions", (), {
        "authorize_request": lambda self, _request: "permission:mode",
    })(), Approval(), invoker, Redactor(), receipts)
    request = ToolGatewayRequest(
        "req-1", "file_write", {"path": "a.txt", "content": "x"},
        ToolScope("worker", allowed_effects=frozenset({"mutation"}), source="worker"),
        ToolPermission(frozenset({"mutation"}), ApprovalMode.NOT_REQUIRED),
    )
    with bound(JournalBinding(journal, "run-1", "w-1", 1, "/workspace")):
        receipt = gateway.execute(request)
        with pytest.raises(EffectJournalError, match="duplicate effect intent"):
            gateway.execute(request)
    assert receipt.success and journal.high_water("run-1") == 1
    assert invoker.calls == 1
    journal.validate_checkpoint("run-1", 1)


def test_gateway_marks_effect_uncertain_when_runner_crashes(tmp_path):
    from sonder_runtime.application.tools.gateway_contract import (
        ToolGateway, ToolGatewayRequest, ToolInvocationOutput, ToolPermission, ToolScope,
    )

    class Schema:
        def validate(self, *_args): pass
    class Permissions:
        def authorize_request(self, _request): return "permission:mode"
    class Approval:
        def approve(self, _request): return True
    class Invoker:
        def invoke(self, _request): raise RuntimeError("runner stopped")
    class Redactor:
        def redact(self, _tool, value): return value
    class Receipts:
        def record(self, _receipt): pass

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    gateway = ToolGateway(Schema(), Permissions(), Approval(), Invoker(), Redactor(), Receipts())
    request = ToolGatewayRequest(
        "req-2", "run_code", {"command": "touch a.txt"},
        ToolScope("worker", allowed_effects=frozenset({"execution"}), source="worker"),
        ToolPermission(frozenset({"execution"})),
    )
    with bound(JournalBinding(journal, "run-1", "w-1", 1, "/workspace")):
        with pytest.raises(RuntimeError):
            gateway.execute(request)
    assert journal.get("run-1:req-2").state is EffectState.UNCERTAIN
