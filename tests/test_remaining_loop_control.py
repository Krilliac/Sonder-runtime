from sonder_runtime.application.cancellation_tree import CancellationTree
from sonder_runtime.application.loop.durable_control import (
    DurableLoopControl,
    IdempotencyConflict,
    OutboxIdempotencyStore,
    RetryEvidenceLedger,
)
from sonder_runtime.application.persistence.outbox_cas import InMemoryOutboxCASRepository
from sonder_runtime.application.ports.specialized_lifecycle import CleanupResult
from sonder_runtime.domain.loop_retry_policy import ReplayAction, SideEffectClass


def test_cancellation_propagates_and_requires_cleanup_conformance():
    tree = CancellationTree()
    child = tree.create_child(node_id="child")
    control = DurableLoopControl(cancellation=tree)
    calls = []

    def cancel(reason):
        calls.append(("cancel", reason))
        return True

    def cleanup(timeout):
        calls.append(("cleanup", timeout))
        return CleanupResult("provider", True, True, "released")

    control.bind("child", "provider", cancel=cancel, cleanup=cleanup)
    reports = control.cancel_and_cleanup("root", reason="stop", timeout=2.0)
    assert child.cancelled
    assert reports[0].conforms
    assert calls == [("cancel", "stop"), ("cleanup", 2.0)]


def test_retry_policy_retains_bounded_evidence_and_blocks_blind_replay():
    ledger = RetryEvidenceLedger(max_records=1, clock=lambda: "t")
    control = DurableLoopControl(ledger=ledger)
    decision = control.retry("op", attempt=1, max_attempts=2, outcome_known=False, effect=SideEffectClass.NON_IDEMPOTENT, idempotency_key="k")
    assert decision.action is ReplayAction.RECONCILE_THEN_RETRY
    control.retry("op", failure_code="timeout", attempt=2, max_attempts=2)
    assert len(ledger.snapshot()) == 1
    assert ledger.snapshot()[0].attempt == 2  # newest evidence is retained within the bound


def test_outbox_idempotency_is_persistent_and_requires_reconciliation():
    repository = InMemoryOutboxCASRepository()
    store = OutboxIdempotencyStore(repository, clock=lambda: "t")
    started = store.begin("key", "fingerprint")
    assert store.begin("key", "fingerprint").revision == started.revision
    assert len(repository.outbox()) == 1
    try:
        store.begin("key", "other")
    except IdempotencyConflict:
        pass
    else:
        raise AssertionError("fingerprint reuse must be rejected")
    unknown = store.mark_unknown("key", "fingerprint", evidence={"dispatch": "lost"})
    assert unknown.status == "unknown"
    reconciled = store.reconcile("key", "fingerprint", {"ok": True}, evidence={"source": "query"})
    assert reconciled.status == "reconciled"
    assert len(repository.outbox()) == 3
    assert store.complete("key", "fingerprint", {"ok": False}).status == "reconciled"
    assert len(repository.outbox()) == 3
