from __future__ import annotations

from dataclasses import replace
import pytest

from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.adapters.execution.process_jobs import DurableProcessEffectVerifier
from sonder_runtime.adapters.persistence.sqlite.runtime_checkpoints import SQLiteRuntimeCheckpointRepository
from sonder_runtime.application.execution.effect_journal import (
    EffectIntent, EffectJournalError, EffectOutcome, EffectState, JournalBinding, bound,
    ReconciliationProof,
)
from sonder_runtime.application.execution.worker_bindings import AuthenticatedWorkerBinding
from sonder_runtime.application.ports.runtime_checkpoints import CheckpointError, RestoreStatus, RuntimeCheckpoint
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus


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


def test_recovered_owner_epoch_fences_old_binding_before_new_intent(tmp_path):
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding, journaled_effect,
    )

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    old = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    journaled_effect(
        old, operation_id="first", idempotency_key="first", request={"n": 1},
        invoke=lambda: {"ok": 1}, receipt_key="receipt-first",
    )
    old.binding().begin_request(
        operation_id="pending", idempotency_key="pending", request_digest="c" * 64,
    )
    current = AuthenticatedWorkerBinding(journal, "run", "worker", 2, "/workspace")
    with pytest.raises(EffectJournalError, match="uncertain"):
        current.recover_before_restart()
    called = []
    with pytest.raises(EffectJournalError, match="stale worker owner epoch"):
        journaled_effect(
            old, operation_id="old-after-recovery", idempotency_key="old-after-recovery",
            request={"n": 2}, invoke=lambda: called.append(True), receipt_key="late",
        )
    assert called == []


def test_outcome_and_checkpoint_roll_back_together_at_checkpoint_cut(tmp_path, monkeypatch):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    binding = JournalBinding(journal, "run", "worker", 1, "/workspace")
    intent = binding.begin_request(
        operation_id="atomic", idempotency_key="atomic", request_digest="a" * 64,
    )
    outcome = EffectOutcome(
        intent.intent_id, EffectState.COMPLETED, "b" * 64, "receipt",
        worker_id="worker", owner_epoch=1,
    )
    original = journal._append_checkpoint_in_transaction

    def fail_checkpoint(*args, **kwargs):
        raise RuntimeError("injected checkpoint crash")

    monkeypatch.setattr(journal, "_append_checkpoint_in_transaction", fail_checkpoint)
    with pytest.raises(RuntimeError, match="checkpoint crash"):
        journal.outcome_and_checkpoint(outcome, {"worker": {"step": 1}})
    assert journal.get(intent.intent_id).state is EffectState.INTENT
    assert journal.restore_checkpoint("run") is None
    monkeypatch.setattr(journal, "_append_checkpoint_in_transaction", original)
    journal.outcome_and_checkpoint(outcome, {"worker": {"step": 1}})
    restored = journal.restore_checkpoint("run")
    assert restored is not None
    assert restored["state"] == {"worker": {"step": 1}}


def test_recovery_refusal_blocks_new_effect_before_invocation(tmp_path):
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding, journaled_effect,
    )

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    old = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    old_intent = old.binding().begin_request(
        operation_id="op-1", idempotency_key="op-1", request_digest="a" * 64,
    )
    old.binding().mark_uncertain(old_intent, detail="crash after external effect")

    current = AuthenticatedWorkerBinding(journal, "run", "worker", 2, "/workspace")
    with pytest.raises(EffectJournalError, match="uncertain effects"):
        current.recover_before_restart()

    invoked = []
    with pytest.raises(EffectJournalError, match="reconciliation"):
        journaled_effect(
            current, operation_id="op-2", idempotency_key="op-2",
            request={"value": "must-not-run"},
            invoke=lambda: invoked.append(True), receipt_key="receipt-op-2",
        )
    assert invoked == []


def test_epoch_advance_cannot_clear_recovery_fence(tmp_path):
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding, journaled_effect,
    )

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    epoch_one = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    intent = epoch_one.binding().begin_request(
        operation_id="op-1", idempotency_key="op-1", request_digest="a" * 64,
    )
    epoch_one.binding().mark_uncertain(intent, detail="crash after effect")
    epoch_two = AuthenticatedWorkerBinding(journal, "run", "worker", 2, "/workspace")
    with pytest.raises(EffectJournalError, match="uncertain effects"):
        epoch_two.recover_before_restart()

    # Advancing the durable owner epoch is not reconciliation and must not
    # reopen admission for a newer worker.
    journal.claim_owner("run", "worker", 3)
    invoked = []
    epoch_three = AuthenticatedWorkerBinding(journal, "run", "worker", 3, "/workspace")
    with pytest.raises(EffectJournalError, match="reconciliation"):
        journaled_effect(
            epoch_three, operation_id="op-2", idempotency_key="op-2",
            request={"value": "must-not-run"},
            invoke=lambda: invoked.append(True), receipt_key="receipt-op-2",
        )
    assert invoked == []


def test_default_worker_checkpoint_projection_is_content_free(tmp_path):
    from sonder_runtime.application.execution.worker_bindings import (
        AuthenticatedWorkerBinding, journaled_effect,
    )

    private_output = "PRIVATE_OUTPUT_CANARY_872193"
    private_prompt = "PRIVATE_PROMPT_CANARY_872193"
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    journaled_effect(
        AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace"),
        operation_id="op", idempotency_key="op", request={"prompt": private_prompt},
        invoke=lambda: private_output, receipt_key="receipt",
    )
    raw = (tmp_path / "effects.db").read_bytes()
    assert private_output.encode() not in raw
    assert private_prompt.encode() not in raw


class _ExternalVerifier:
    verifier_id = "external-api-v1"
    operation_ids = frozenset({"reconcile-op"})

    def __init__(self, proof_factory=None):
        self.proof_factory = proof_factory

    def verify(self, intent):
        if self.proof_factory is not None:
            return self.proof_factory(intent)
        return ReconciliationProof(
            intent_id=intent.intent_id, operation_id=intent.operation_id,
            receipt_key="external-receipt-1", outcome_digest="d" * 64,
            state=EffectState.COMPLETED, verifier_id=self.verifier_id,
            external_reference="external-op-1",
        )


def _uncertain_for_reconciliation(tmp_path, operation="reconcile-op"):
    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={"reconcile-op": _ExternalVerifier()},
    )
    first = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    intent = first.binding().begin_request(
        operation_id=operation, idempotency_key="reconcile-key", request_digest="a" * 64,
    )
    first.binding().mark_uncertain(intent, detail="crash after external call")
    journal.claim_owner("run", "worker", 2)
    journal.recover("run", live_workers={})
    return journal, intent


def test_host_verifier_reconciles_exact_effect_and_clears_epoch_fence(tmp_path):
    journal, intent = _uncertain_for_reconciliation(tmp_path)
    resolved = journal.reconcile(intent.intent_id, owner_epoch=2)
    assert resolved.state is EffectState.COMPLETED
    assert resolved.operation_id == "reconcile-op"
    assert resolved.receipt_key == "external-receipt-1"
    resumed = AuthenticatedWorkerBinding(journal, "run", "worker", 2, "/workspace")
    assert resumed.binding().begin_request(
        operation_id="after-reconcile", idempotency_key="after-reconcile",
        request_digest="b" * 64,
    ).state is EffectState.INTENT


def test_unsupported_operation_family_stays_fenced(tmp_path):
    journal, intent = _uncertain_for_reconciliation(tmp_path, operation="unsupported-op")
    with pytest.raises(EffectJournalError, match="no trusted reconciliation verifier"):
        journal.reconcile(intent.intent_id, owner_epoch=2)
    with pytest.raises(EffectJournalError, match="duplicate effect intent"):
        journal.begin(EffectIntent(
            "run:after", "run", "worker", "after", "/workspace", 2,
            "after", "b" * 64,
        ))


def test_stale_epoch_cannot_reconcile_after_restart(tmp_path):
    journal, intent = _uncertain_for_reconciliation(tmp_path)
    with pytest.raises(EffectJournalError, match="stale reconciliation owner epoch"):
        journal.reconcile(intent.intent_id, owner_epoch=1)
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN


def test_conflicting_verifier_proof_is_rejected_and_fence_remains(tmp_path):
    verifier = _ExternalVerifier(
        lambda intent: ReconciliationProof(
            intent_id="wrong", operation_id=intent.operation_id,
            receipt_key="receipt", outcome_digest="e" * 64,
            state=EffectState.COMPLETED, verifier_id="external-api-v1",
            external_reference="external-op",
        ),
    )
    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={"reconcile-op": verifier},
    )
    first = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    intent = first.binding().begin_request(
        operation_id="reconcile-op", idempotency_key="key", request_digest="a" * 64,
    )
    first.binding().mark_uncertain(intent, detail="crash")
    journal.claim_owner("run", "worker", 2)
    with pytest.raises(EffectJournalError, match="proof identity conflict"):
        journal.reconcile(intent.intent_id, owner_epoch=2)
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN


def test_reconciliation_is_idempotent_under_replay_and_concurrent_call(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    journal, intent = _uncertain_for_reconciliation(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _: journal.reconcile(intent.intent_id, owner_epoch=2), range(2),
        ))
    assert {result.state for result in results} == {EffectState.COMPLETED}
    replay = journal.reconcile(intent.intent_id, owner_epoch=2)
    assert replay.receipt_key == "external-receipt-1"


def test_post_construction_verifier_registration_is_unavailable(tmp_path):
    journal, intent = _uncertain_for_reconciliation(tmp_path, operation="unsupported-op")
    assert not hasattr(journal, "register_reconciliation_verifier")
    with pytest.raises(EffectJournalError, match="no trusted reconciliation verifier"):
        journal.reconcile(intent.intent_id, owner_epoch=2)


def test_hung_verifier_does_not_block_journal_writes(tmp_path):
    import threading
    started = threading.Event()

    class HungVerifier(_ExternalVerifier):
        def verify(self, intent):
            started.set()
            threading.Event().wait(0.5)

    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={"reconcile-op": HungVerifier()},
    )

    first = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    intent = first.binding().begin_request(
        operation_id="reconcile-op", idempotency_key="key", request_digest="a" * 64,
    )
    first.binding().mark_uncertain(intent, detail="crash")
    journal.claim_owner("run", "worker", 2)
    with pytest.raises(EffectJournalError, match="timed out"):
        journal.reconcile(intent.intent_id, owner_epoch=2, timeout_seconds=0.05)
    assert started.is_set()
    # A separate write is immediately available despite the hung verifier.
    assert journal.high_water("run") == 1


def test_owner_epoch_race_invalidates_proof_before_atomic_clear(tmp_path):
    import threading
    started = threading.Event()
    release = threading.Event()

    class PausedVerifier(_ExternalVerifier):
        def verify(self, intent):
            started.set()
            release.wait(2)
            return super().verify(intent)

    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={"reconcile-op": PausedVerifier()},
    )

    first = AuthenticatedWorkerBinding(journal, "run", "worker", 1, "/workspace")
    intent = first.binding().begin_request(
        operation_id="reconcile-op", idempotency_key="key", request_digest="a" * 64,
    )
    first.binding().mark_uncertain(intent, detail="crash")
    journal.claim_owner("run", "worker", 2)
    errors = []

    def reconcile():
        try:
            journal.reconcile(intent.intent_id, owner_epoch=2)
        except EffectJournalError as exc:
            errors.append(str(exc))

    thread = threading.Thread(target=reconcile)
    thread.start()
    assert started.wait(1)
    journal.claim_owner("run", "worker", 3)
    release.set()
    thread.join(2)
    assert errors == ["stale reconciliation owner epoch"]
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN


def test_verifier_admission_is_bounded_across_journal_instances(tmp_path):
    from sonder_runtime.adapters.persistence.sqlite import effect_journal as module
    held = [module._VERIFIER_SLOTS.acquire(timeout=2) for _ in range(4)]
    assert all(held)
    try:
        journal, intent = _uncertain_for_reconciliation(tmp_path)
        with pytest.raises(EffectJournalError, match="capacity exhausted"):
            journal.reconcile(intent.intent_id, owner_epoch=2, timeout_seconds=0.1)
    finally:
        for acquired in held:
            if acquired:
                module._VERIFIER_SLOTS.release()


def test_production_process_registry_verifier_reconciles_after_restart(tmp_path):
    class Registry:
        def __init__(self, status):
            self.status = status

        def poll(self, job_id):
            return JobRecord(
                JobIdentity(job_id, "process", "launch", job_id),
                self.status, revision=4,
            )

        def view(self, job_id):
            return type("View", (), {
                "record": self.poll(job_id),
                "metadata": {"process_request_digest": "a" * 64},
            })()

    registry = Registry(JobStatus.SUCCEEDED)
    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={
            "process-start": DurableProcessEffectVerifier(lambda: registry),
        },
    )
    old = AuthenticatedWorkerBinding(journal, "run", "process", 1, "/workspace")
    intent = old.binding().begin_request(
        operation_id="process-start:job-1", idempotency_key="job-1",
        request_digest="a" * 64, reconciliation="idempotent",
    )
    old.binding().mark_uncertain(intent, detail="crash after process launch")
    current = AuthenticatedWorkerBinding(journal, "run", "process", 2, "/workspace")
    with pytest.raises(EffectJournalError, match="explicit reconciliation"):
        current.recover_before_restart()
    resolved = journal.reconcile(intent.intent_id, owner_epoch=2)
    assert resolved.state is EffectState.COMPLETED
    assert resolved.receipt_key == "process-job:job-1:4"

    registry.status = JobStatus.PENDING
    old_two = AuthenticatedWorkerBinding(journal, "run-2", "process", 1, "/workspace")
    pending = old_two.binding().begin_request(
        operation_id="process-start:job-2", idempotency_key="job-2",
        request_digest="a" * 64, reconciliation="idempotent",
    )
    old_two.binding().mark_uncertain(pending, detail="unknown process outcome")
    current_two = AuthenticatedWorkerBinding(journal, "run-2", "process", 2, "/workspace")
    with pytest.raises(EffectJournalError, match="explicit reconciliation"):
        current_two.recover_before_restart()
    with pytest.raises(EffectJournalError, match="host verifier returned no trusted proof"):
        journal.reconcile(pending.intent_id, owner_epoch=2)


def test_process_verifier_rejects_durable_identity_mismatch(tmp_path):
    class Registry:
        def poll(self, job_id):
            return JobRecord(
                JobIdentity(job_id, "unrelated-kind", "different-operation", "other-idempotency"),
                JobStatus.SUCCEEDED, revision=3,
            )

    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={
            "process-start": DurableProcessEffectVerifier(lambda: Registry()),
        },
    )
    old = AuthenticatedWorkerBinding(journal, "run", "process", 1, "/workspace")
    intent = old.binding().begin_request(
        operation_id="process-start:job-1", idempotency_key="expected",
        request_digest="a" * 64, reconciliation="idempotent",
    )
    old.binding().mark_uncertain(intent, detail="crash after process launch")
    current = AuthenticatedWorkerBinding(journal, "run", "process", 2, "/workspace")
    with pytest.raises(EffectJournalError, match="explicit reconciliation"):
        current.recover_before_restart()
    with pytest.raises(EffectJournalError, match="host verifier returned no trusted proof"):
        journal.reconcile(intent.intent_id, owner_epoch=2)
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN


def test_process_verifier_rejects_same_identity_with_changed_request_digest(tmp_path):
    class Registry:
        def view(self, job_id):
            identity = JobIdentity(job_id, "process", "launch", "expected")
            record = JobRecord(identity, JobStatus.SUCCEEDED, revision=3)
            return type("View", (), {
                "record": record,
                "metadata": {"process_request_digest": "a" * 64},
            })()

    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={
            "process-start": DurableProcessEffectVerifier(lambda: Registry()),
        },
    )
    old = AuthenticatedWorkerBinding(journal, "run", "process", 1, "/workspace")
    intent = old.binding().begin_request(
        operation_id="process-start:job-1", idempotency_key="expected",
        request_digest="b" * 64, reconciliation="idempotent",
    )
    old.binding().mark_uncertain(intent, detail="crash after process launch")
    current = AuthenticatedWorkerBinding(journal, "run", "process", 2, "/workspace")
    with pytest.raises(EffectJournalError, match="explicit reconciliation"):
        current.recover_before_restart()
    with pytest.raises(EffectJournalError, match="host verifier returned no trusted proof"):
        journal.reconcile(intent.intent_id, owner_epoch=2)
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN
