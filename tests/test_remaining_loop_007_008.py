from sonder_runtime.adapters.persistence.sqlite.loop_state import (
    SQLiteLoopStateRepository,
    SQLiteRetryEvidenceLedger,
)
from sonder_runtime.application.loop.durable_control import OutboxIdempotencyStore
from sonder_runtime.domain.loop_retry_policy import SideEffectClass, retry_decision


def test_sqlite_idempotency_survives_new_store_and_replays_terminal_result(tmp_path):
    db = tmp_path / "loop.db"
    first = OutboxIdempotencyStore(SQLiteLoopStateRepository(db), clock=lambda: "t")
    assert first.begin("key", "fp").status == "started"
    assert first.mark_unknown("key", "fp", evidence={"dispatch": "lost"}).status == "unknown"

    second = OutboxIdempotencyStore(SQLiteLoopStateRepository(db), clock=lambda: "t")
    reconciled = second.reconcile("key", "fp", {"ok": True}, evidence={"source": "query"})
    assert reconciled.status == "reconciled"
    assert second.complete("key", "fp", {"ok": False}).result == {"ok": True}
    assert len(SQLiteLoopStateRepository(db).outbox()) == 3


def test_sqlite_idempotency_rejects_fingerprint_conflict(tmp_path):
    store = OutboxIdempotencyStore(SQLiteLoopStateRepository(tmp_path / "loop.db"))
    store.begin("key", "fp")
    try:
        store.begin("key", "other")
    except ValueError as exc:
        assert "different operation" in str(exc)
    else:
        raise AssertionError("fingerprint conflict must be rejected")


def test_sqlite_retry_evidence_is_retained_across_ledger_instances(tmp_path):
    db = tmp_path / "loop.db"
    ledger = SQLiteRetryEvidenceLedger(db, max_records=2, clock=lambda: "t")
    for attempt in (1, 2, 3):
        ledger.record(
            "op", retry_decision("timeout", attempt=attempt, max_attempts=3),
            attempt=attempt, failure_code="timeout",
        )
    reopened = SQLiteRetryEvidenceLedger(db, max_records=2, clock=lambda: "t")
    assert [item.attempt for item in reopened.snapshot()] == [2, 3]
    assert reopened.snapshot()[0].evidence_digest == ledger.snapshot()[0].evidence_digest


def test_persistent_retry_evidence_preserves_reconciliation_decision(tmp_path):
    ledger = SQLiteRetryEvidenceLedger(tmp_path / "loop.db", clock=lambda: "t")
    decision = retry_decision(
        "timeout", attempt=1, max_attempts=2, outcome_known=False,
        effect=SideEffectClass.NON_IDEMPOTENT, idempotency_key="key",
    )
    evidence = ledger.record("op", decision, failure_code="timeout")
    assert evidence.action.value == "reconcile_then_retry"
    assert ledger.snapshot()[0].classification == "unknown_outcome"
