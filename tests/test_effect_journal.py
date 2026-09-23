from __future__ import annotations

from dataclasses import replace
import pytest

from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import SQLiteRuntimeCheckpointRepository
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent, EffectJournalError, EffectOutcome, EffectState, JournalBinding, bound,
)
from sonder_runtime.application.ports.runtime_checkpoints import CheckpointError, RestoreStatus, RuntimeCheckpoint


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
    assert decision.action == "reconcile"
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


def test_uncertain_effect_never_reattaches_even_if_old_owner_is_live(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    journal.begin(_intent(worker_id="worker"))
    journal.uncertain("i-1", detail="worker stopped after external call")

    decision = journal.recover("run-1", live_workers={"worker": 2})

    assert decision.action == "reconcile"
    assert decision.intent_ids == ("i-1",)
    assert journal.get("i-1").state is EffectState.UNCERTAIN


def test_uncertain_effect_blocks_an_otherwise_live_intent(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    journal.begin(_intent("live", worker_id="worker", key="live"))
    journal.begin(_intent("uncertain", worker_id="worker", key="uncertain"))
    journal.uncertain("uncertain", detail="external outcome unknown")

    decision = journal.recover("run-1", live_workers={"worker": 2})

    assert decision.action == "reconcile"
    assert decision.intent_ids == ("live", "uncertain")


def test_crash_cut_points_never_turn_an_unresolved_effect_into_completion(tmp_path):
    """Persisted journal state is authoritative at each worker crash boundary."""
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = JournalBinding(journal, "run-1", "worker", 2, "/workspace")

    # Crash before invocation: no admission exists, so recovery is a no-op.
    assert journal.recover("run-1", live_workers={}).action == "resume"

    # Crash during invocation / after the external effect but before its
    # receipt: an admitted intent remains uncertain and cannot be replayed.
    intent = binding.begin_request(
        operation_id="mutate", idempotency_key="mutate-1", request_digest="a" * 64,
    )
    assert intent.state is EffectState.INTENT
    reopened = SQLiteEffectJournal(tmp_path / "effects.db")
    decision = reopened.recover("run-1", live_workers={})
    assert decision.action == "reconcile"
    assert reopened.get(intent.intent_id).state is EffectState.UNCERTAIN
    with pytest.raises(EffectJournalError, match="duplicate effect intent"):
        JournalBinding(reopened, "run-1", "worker", 2, "/workspace").begin_request(
            operation_id="mutate", idempotency_key="mutate-1", request_digest="a" * 64,
        )

    # A durable receipt before a checkpoint is terminal and cannot be
    # mistaken for a second invocation after restart.
    terminal = SQLiteEffectJournal(tmp_path / "terminal.db")
    terminal_binding = JournalBinding(terminal, "run-2", "worker", 2, "/workspace")
    terminal_intent = terminal_binding.begin_request(
        operation_id="mutate", idempotency_key="mutate-2", request_digest="b" * 64,
    )
    terminal_binding.complete(
        terminal_intent, outcome_digest="c" * 64, receipt_key="receipt-2",
    )
    replay = terminal.begin(terminal_intent)
    assert replay.replayed is True
    assert replay.state is EffectState.COMPLETED


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
    journal.begin(_intent("after-checkpoint", key="after-checkpoint"))
    journal.outcome(EffectOutcome(
        "after-checkpoint", EffectState.COMPLETED, "c" * 64,
        "receipt-after", worker_id="w-1", owner_epoch=2,
    ))
    assert checkpoints.restore("run-1").status is RestoreStatus.CORRUPT


def test_checkpoint_refuses_a_separate_effect_database(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    with pytest.raises(CheckpointError, match="share"):
        SQLiteRuntimeCheckpointRepository(
            tmp_path / "checkpoints.db", seal_key=b"k" * 32,
            effect_journal=journal,
        )


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


def test_worker_effect_checkpoint_is_bound_to_terminal_high_water(tmp_path):
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding, journaled_effect,
    )

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = AuthenticatedWorkerBinding(journal, "worker-run", "worker", 1, "/workspace")
    journaled_effect(
        binding, operation_id="write-1", idempotency_key="write-1",
        request={"value": "one"}, invoke=lambda: {"receipt": "one"},
        receipt_key="receipt-one",
    )
    first = journal.restore_checkpoint("worker-run")
    assert first is not None
    assert first["generation"] == 0
    assert first["effect_high_water"] == 1
    journaled_effect(
        binding, operation_id="write-2", idempotency_key="write-2",
        request={"value": "two"}, invoke=lambda: {"receipt": "two"},
        receipt_key="receipt-two",
    )
    second = journal.restore_checkpoint("worker-run")
    assert second is not None
    assert second["generation"] == 1
    assert second["effect_high_water"] == 2


def test_worker_checkpoint_rejects_effect_admitted_after_last_checkpoint(tmp_path):
    from sonder_runtime.application.execution.worker_bindings import AuthenticatedWorkerBinding

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = JournalBinding(journal, "worker-run", "worker", 1, "/workspace")
    first = binding.begin_request(
        operation_id="first", idempotency_key="first", request_digest="a" * 64,
    )
    binding.complete(first, outcome_digest="b" * 64, receipt_key="receipt-first")
    journal.append_checkpoint("worker-run", "state-one")
    second = binding.begin_request(
        operation_id="second", idempotency_key="second", request_digest="c" * 64,
    )
    binding.complete(second, outcome_digest="d" * 64, receipt_key="receipt-second")
    with pytest.raises(EffectJournalError, match="high-water is stale"):
        journal.restore_checkpoint("worker-run")
    with pytest.raises(EffectJournalError, match="checkpoint reconciliation"):
        AuthenticatedWorkerBinding(
            journal, "worker-run", "worker", 2, "/workspace",
        ).recover_before_restart()
