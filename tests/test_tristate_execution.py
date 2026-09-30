"""Exercise physical retry and speculation without filesystem dependencies."""
from dataclasses import replace
from threading import Event, Thread, current_thread
from types import SimpleNamespace

import pytest

from sonder_runtime.application.loop.durable_control import IdempotencyReceipt, RetryEvidenceLedger
from sonder_runtime.application.loop.transport_retry import (
    TransportRetryExecutor, TransportFailure, RetryExecutionError,
    ReconciliationResult, ReconciliationState,
)
from sonder_runtime.domain.loop_retry_policy import SideEffectClass, retry_decision, ReplayAction
from sonder_runtime.domain.speculation_policy import is_speculatable
from sonder_runtime.domain.tools.traits import ToolTraits, TriState
from sonder_speculation import SpeculationEngine


class MemoryReceipts:
    """A port fake; the retry executor and policy remain real."""
    receipt = None

    def begin(self, key, fingerprint):
        self.receipt = IdempotencyReceipt(key, fingerprint, "started", 1)
        return self.receipt

    def complete(self, key, fingerprint, result):
        self.receipt = replace(self.receipt, status="completed", result=result)
        return self.receipt

    def mark_unknown(self, key, fingerprint, *, evidence):
        self.receipt = replace(self.receipt, status="unknown")
        return self.receipt

    def reconcile(self, key, fingerprint, result, *, evidence):
        return self.complete(key, fingerprint, result)


class FlakyTransport:
    def __init__(self, outcome_known, committed=False):
        self.calls = 0
        self.queries = 0
        self.outcome_known = outcome_known
        self.committed = committed

    def send(self, request, *, idempotency_key, attempt):
        self.calls += 1
        if self.calls == 1:
            raise TransportFailure("timeout", outcome_known=self.outcome_known)
        return "done"

    def reconcile(self, request, *, idempotency_key):
        self.queries += 1
        return ReconciliationResult(
            ReconciliationState.COMMITTED if self.committed else ReconciliationState.RETRY_SAFE,
            "prior result", {"query": "verified"},
        )


def executor(transport):
    return TransportRetryExecutor(transport, idempotency=MemoryReceipts(),
                                  evidence=RetryEvidenceLedger(), sleep=lambda delay: None)


@pytest.mark.parametrize("outcome_known", [True, False])
@pytest.mark.parametrize("traits,effect", [
    (ToolTraits(), SideEffectClass.IDEMPOTENT),
    (ToolTraits(idempotent=TriState.FALSE), SideEffectClass.IDEMPOTENT),
    (ToolTraits(idempotent=TriState.TRUE, host_declared=False), SideEffectClass.IDEMPOTENT),
    (None, SideEffectClass.NON_IDEMPOTENT),
    (None, "non_idempotent"),
    (ToolTraits(idempotent=TriState.TRUE), SideEffectClass.NON_IDEMPOTENT),
])
def test_no_second_send_without_replay_authority(traits, effect, outcome_known):
    transport = FlakyTransport(outcome_known)
    with pytest.raises(RetryExecutionError):
        executor(transport).execute("op", {}, fingerprint="fp", idempotency_key="key",
                                    traits=traits, effect=effect)
    assert transport.calls == 1


def test_host_idempotent_declaration_allows_retry():
    transport = FlakyTransport(True)
    result = executor(transport).execute(
        "op", {}, fingerprint="fp", idempotency_key="key",
        traits=ToolTraits(idempotent=TriState.TRUE),
    )
    assert result.result == "done" and transport.calls == 2


def test_reconciliation_can_return_committed_result_without_replay():
    transport = FlakyTransport(False, committed=True)
    result = executor(transport).execute(
        "op", {}, fingerprint="fp", idempotency_key="key", traits=ToolTraits(),
    )
    assert result.result == "prior result" and transport.calls == 1


def test_replay_trait_does_not_override_deadline_or_missing_key():
    assert retry_decision("timeout", effect="non_idempotent").action is ReplayAction.DO_NOT_RETRY
    assert retry_decision("timeout", outcome_known=False, deadline_seconds=0,
                          traits=ToolTraits()).action is ReplayAction.DO_NOT_RETRY


def predictor():
    # Accounting is observational; use real admission policy and worker engine.
    return SimpleNamespace(speculatable=is_speculatable, note_speculation=lambda: None,
                           note_squash=lambda: None, note_hit=lambda *args: None)


def test_external_read_only_cannot_speculate_even_when_name_matches_builtin():
    calls = []
    engine = SpeculationEngine(predictor(), lambda *args: calls.append(args))
    assert not engine.begin("file_read", "call", {}, traits=ToolTraits(
        read_only=TriState.TRUE, host_declared=False,
    ))
    assert not calls


def test_unknown_concurrency_stays_reserved_until_detached_worker_exits(monkeypatch):
    started, release = Event(), Event()
    joining = Event()
    workers = []

    def dispatch(name, arguments):
        workers.append(current_thread())
        started.set()
        assert release.wait(3)
        return "read", True

    engine = SpeculationEngine(predictor(), dispatch, slots=2)
    assert engine.begin("workspace_inventory", "first", {})
    assert started.wait(3)
    original_join = workers[0].join
    def observe_join(timeout=None):
        joining.set()
        return original_join(timeout)
    monkeypatch.setattr(workers[0], "join", observe_join)
    # Discarding/invalidation removes the slot before joining the still live
    # worker. Coordinate on the engine lock to observe that precise state.
    invalidating = Thread(target=engine.invalidate)
    invalidating.start()
    try:
        assert joining.wait(3)
        assert not engine.begin("status", "second", {})
    finally:
        release.set()
        invalidating.join(3)
    assert not invalidating.is_alive()
    assert engine.begin("status", "fresh", {})
    engine.invalidate()


def test_default_builtin_parallel_declarations_allow_two_physical_reads():
    entered = {"file_read": Event(), "text_search": Event()}
    release = Event()

    def dispatch(name, arguments):
        entered[name].set()
        assert release.wait(3)
        return "read", True

    engine = SpeculationEngine(predictor(), dispatch, slots=2)
    try:
        assert engine.begin("file_read", "read", {})
        assert entered["file_read"].wait(3)
        assert engine.begin("text_search", "search", {})
        assert entered["text_search"].wait(3)
    finally:
        release.set()
        engine.invalidate()
