from sonder_runtime.adapters.persistence.sqlite.loop_state import (
    SQLiteLoopStateRepository,
    SQLiteRetryEvidenceLedger,
)
from sonder_runtime.application.cancellation_tree import CancellationTree
from sonder_runtime.application.loop.durable_control import OutboxIdempotencyStore
from sonder_runtime.application.loop.transport_retry import (
    ReconciliationResult,
    ReconciliationState,
    RetryCancelled,
    RetryExecutionError,
    TransportFailure,
    TransportRetryExecutor,
)
from sonder_runtime.domain.loop_retry_policy import SideEffectClass


class ScriptedTransport:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.reconciliations = []

    def send(self, request, *, idempotency_key, attempt):
        self.calls.append((request, idempotency_key, attempt))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def reconcile(self, request, *, idempotency_key):
        self.reconciliations.append((request, idempotency_key))
        return ReconciliationResult(ReconciliationState.RETRY_SAFE, evidence={"source": "query"})


def make_executor(tmp_path, transport, *, cancellation=None, sleep=None):
    db = tmp_path / "loop.db"
    return TransportRetryExecutor(
        transport,
        idempotency=OutboxIdempotencyStore(SQLiteLoopStateRepository(db)),
        evidence=SQLiteRetryEvidenceLedger(db, clock=lambda: "t"),
        cancellation=cancellation,
        sleep=sleep or (lambda seconds: None),
    )


def test_typed_transport_retries_with_one_key_and_durable_evidence(tmp_path):
    transport = ScriptedTransport(
        TransportFailure("timeout", outcome_known=True),
        {"ok": True},
    )
    delays = []
    executor = make_executor(tmp_path, transport, sleep=delays.append)

    result = executor.execute(
        "op", {"value": 1}, fingerprint="fp", idempotency_key="stable",
        max_attempts=2,
    )

    assert result.result == {"ok": True}
    assert result.attempts == 2
    assert [call[1] for call in transport.calls] == ["stable", "stable"]
    assert [call[2] for call in transport.calls] == [1, 2]
    assert delays == [0.25]
    assert len(result.evidence) == 1
    assert result.evidence[0].classification == "transient"


def test_unknown_outcome_reconciles_before_effectful_replay(tmp_path):
    transport = ScriptedTransport(
        TransportFailure("timeout"),
        {"ok": "replayed"},
    )
    executor = make_executor(tmp_path, transport)

    result = executor.execute(
        "op", "request", fingerprint="fp", idempotency_key="stable",
        max_attempts=2, effect=SideEffectClass.NON_IDEMPOTENT,
    )

    assert result.result == {"ok": "replayed"}
    assert transport.reconciliations == [("request", "stable")]
    assert len(result.evidence) == 1
    assert result.evidence[0].action.value == "reconcile_then_retry"


def test_committed_reconciliation_returns_without_replay(tmp_path):
    class Committed(ScriptedTransport):
        def reconcile(self, request, *, idempotency_key):
            self.reconciliations.append((request, idempotency_key))
            return ReconciliationResult(
                ReconciliationState.COMMITTED, {"already": True}, {"source": "query"}
            )

    transport = Committed(TransportFailure("connection-reset"))
    result = make_executor(tmp_path, transport).execute(
        "op", "request", fingerprint="fp", idempotency_key="stable", max_attempts=3,
        effect=SideEffectClass.IDEMPOTENT,
    )

    assert result.result == {"already": True}
    assert result.attempts == 1
    assert len(transport.calls) == 1
    assert len(transport.reconciliations) == 1


def test_untyped_transport_failure_is_unknown_and_fails_closed(tmp_path):
    class Untyped(ScriptedTransport):
        def send(self, request, *, idempotency_key, attempt):
            self.calls.append((request, idempotency_key, attempt))
            raise RuntimeError("ambiguous")

    transport = Untyped()
    executor = make_executor(tmp_path, transport)
    try:
        executor.execute("op", "request", fingerprint="fp", idempotency_key="stable")
    except RetryExecutionError as exc:
        assert "untyped" in str(exc)
    else:
        raise AssertionError("untyped failures must fail closed")
    assert len(transport.calls) == 1


def test_cancellation_prevents_the_next_attempt(tmp_path):
    tree = CancellationTree()
    transport = ScriptedTransport(TransportFailure("timeout", outcome_known=True), {"bad": True})

    def cancel_during_wait(_seconds):
        tree.cancel(reason="stop")

    executor = make_executor(tmp_path, transport, cancellation=tree.root, sleep=cancel_during_wait)
    try:
        executor.execute("op", "request", fingerprint="fp", idempotency_key="stable", max_attempts=2)
    except RetryCancelled:
        pass
    else:
        raise AssertionError("cancellation must stop before another send")
    assert len(transport.calls) == 1


def test_completed_idempotent_operation_is_replayed_from_durable_state(tmp_path):
    transport = ScriptedTransport({"ok": True})
    executor = make_executor(tmp_path, transport)
    first = executor.execute("op", "request", fingerprint="fp", idempotency_key="stable")
    second_transport = ScriptedTransport({"should-not-send": True})
    second = make_executor(tmp_path, second_transport).execute(
        "op", "request", fingerprint="fp", idempotency_key="stable"
    )

    assert first.result == second.result == {"ok": True}
    assert second.replayed is True
    assert second_transport.calls == []
