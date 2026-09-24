"""Regression tests for the PR #523 review findings (P1-1, P1-2, P2-1, P2-2, P3).

Each test reproduces a reviewer scenario that failed on 3d22e670.
"""
from __future__ import annotations

import sqlite3
import threading

import pytest

from sonder_runtime.adapters.execution.process_jobs import DurableProcessEffectVerifier
from sonder_runtime.adapters.persistence.sqlite.effect_journal import SQLiteEffectJournal
from sonder_runtime.application.execution.effect_journal import (
    EffectJournalError, EffectOutcome, EffectState, ReconciliationProof,
)
from sonder_runtime.application.execution.worker_bindings import (
    AuthenticatedWorkerBinding, journaled_effect,
)
from sonder_runtime.application.ports.jobs import JobIdentity, JobRecord, JobStatus


class _Verifier:
    verifier_id = "test-verifier"
    operation_ids = frozenset({"op"})

    def verify(self, intent):
        return ReconciliationProof(
            intent.intent_id, intent.operation_id, "verified-receipt", "d" * 64,
            EffectState.COMPLETED, self.verifier_id, "external-ref",
        )


# --- P1-1 -----------------------------------------------------------------

def test_overlapping_effects_in_one_run_both_publish_interleaved(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    context = AuthenticatedWorkerBinding(
        journal, "runtime:process-jobs", "process:n1", 5, "process-jobs",
    )
    ran = []

    def invoke_a():
        ran.append("A")
        # Another start is admitted while A is still in flight.
        context.binding().begin_request(
            operation_id="process-start:B", idempotency_key="kB", request_digest="b" * 64,
        )
        return {"pid": 1}

    journaled_effect(
        context, operation_id="process-start:A", idempotency_key="kA",
        request={"a": 1}, invoke=invoke_a, receipt_key="A:1",
    )
    assert ran == ["A"]
    assert journal.get("runtime:process-jobs:process-start:A").state is EffectState.COMPLETED
    checkpoint_a = journal.restore_checkpoint  # B is still open: restore refuses
    with pytest.raises(EffectJournalError):
        checkpoint_a("runtime:process-jobs")
    # A's checkpoint binds only the settled prefix (A), not in-flight B.
    with sqlite3.connect(tmp_path / "effects.db") as connection:
        assert connection.execute(
            "SELECT MAX(effect_high_water) FROM effect_checkpoint"
        ).fetchone()[0] == 1


def test_concurrent_threads_in_one_run_never_turn_launched_effects_uncertain(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    context = AuthenticatedWorkerBinding(
        journal, "runtime:compute-jobs", "compute:n1", 3, "compute-jobs",
    )
    count = 8
    both_admitted = threading.Barrier(count)
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        def invoke():
            both_admitted.wait(timeout=10)  # every intent is open at once
            return {"n": index}
        try:
            journaled_effect(
                context, operation_id=f"compute-submit:{index}",
                idempotency_key=f"k{index}", request={"n": index},
                invoke=invoke, receipt_key=f"r{index}",
            )
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert errors == []
    states = {
        journal.get(f"runtime:compute-jobs:compute-submit:{i}").state for i in range(count)
    }
    assert states == {EffectState.COMPLETED}
    assert journal.settled_high_water("runtime:compute-jobs") == count
    AuthenticatedWorkerBinding(
        journal, "runtime:compute-jobs", "compute:n1", 4, "compute-jobs",
    ).recover_before_restart()


# --- P1-2 -----------------------------------------------------------------

def test_restart_succeeds_after_verified_reconciliation(tmp_path):
    journal = SQLiteEffectJournal(
        tmp_path / "effects.db", reconciliation_verifiers={"op": _Verifier()},
    )
    first = AuthenticatedWorkerBinding(journal, "run", "w", 1, "s")
    journaled_effect(
        first, operation_id="op:1", idempotency_key="k1", request=1,
        invoke=lambda: 1, receipt_key="r1",
    )
    first.binding().begin_request(
        operation_id="op:2", idempotency_key="k2", request_digest="x" * 64,
    )  # crash here
    second = AuthenticatedWorkerBinding(journal, "run", "w", 2, "s")
    with pytest.raises(EffectJournalError, match="reconciliation"):
        second.recover_before_restart()
    assert journal.reconcile("run:op:2", owner_epoch=2).state is EffectState.COMPLETED

    decision = second.recover_before_restart()
    assert decision.action == "resume"
    restored = journal.restore_checkpoint("run")
    assert restored["effect_high_water"] == 1
    assert restored["journal_high_water"] == 2
    # The resuming worker learns about the reconciled effect from the journal.
    page = journal.effects_since("run", restored["effect_high_water"])
    assert [(r.intent_id, r.state) for r in page.records] == [
        ("run:op:2", EffectState.COMPLETED),
    ]


def test_restore_refuses_checkpoint_ahead_of_journal(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    context = AuthenticatedWorkerBinding(journal, "run", "w", 1, "s")
    journaled_effect(
        context, operation_id="op:1", idempotency_key="k1", request=1,
        invoke=lambda: 1, receipt_key="r1",
    )
    with sqlite3.connect(tmp_path / "effects.db") as connection:
        connection.execute("UPDATE effect_checkpoint SET effect_high_water=9")
    with pytest.raises(EffectJournalError, match="ahead of the journal"):
        journal.restore_checkpoint("run")


# --- P2-1 -----------------------------------------------------------------

def test_other_workers_in_run_are_unfenced_once_everything_is_settled(tmp_path):
    journal = SQLiteEffectJournal(
        tmp_path / "effects.db", reconciliation_verifiers={"op": _Verifier()},
    )
    a1 = AuthenticatedWorkerBinding(journal, "run", "wa", 1, "s")
    b1 = AuthenticatedWorkerBinding(journal, "run", "wb", 1, "s")
    b0 = b1.binding().begin_request(
        operation_id="op:b0", idempotency_key="kb0", request_digest="x" * 64,
    )
    journal.outcome(EffectOutcome(
        b0.intent_id, EffectState.COMPLETED, "d" * 64, "r", worker_id="wb", owner_epoch=1,
    ))
    a1.binding().begin_request(
        operation_id="op:a1", idempotency_key="ka1", request_digest="x" * 64,
    )  # crash
    a2 = AuthenticatedWorkerBinding(journal, "run", "wa", 2, "s")
    with pytest.raises(EffectJournalError):
        a2.recover_before_restart()
    with pytest.raises(EffectJournalError):  # fenced while wa is unresolved
        b1.binding().begin_request(
            operation_id="op:b-early", idempotency_key="kb-early", request_digest="x" * 64,
        )
    journal.reconcile("run:op:a1", owner_epoch=2)

    admitted = b1.binding().begin_request(
        operation_id="op:b1", idempotency_key="kb1", request_digest="x" * 64,
    )
    assert admitted.state is EffectState.INTENT


# --- P2-2 -----------------------------------------------------------------

def test_stale_owner_cannot_append_checkpoint_after_newer_owner_claims(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    first = AuthenticatedWorkerBinding(journal, "run", "w", 1, "s")
    journaled_effect(
        first, operation_id="op:1", idempotency_key="k1", request=1,
        invoke=lambda: 1, receipt_key="r1", checkpoint_state={"v": "owner1"},
    )
    AuthenticatedWorkerBinding(journal, "run", "w", 2, "s").recover_before_restart()
    with pytest.raises(EffectJournalError, match="stale worker owner epoch"):
        journal.append_checkpoint(
            "run", {"v": "STALE owner1 write"}, worker_id="w", owner_epoch=1,
        )
    assert journal.restore_checkpoint("run")["state"] == {"v": "owner1"}


def test_checkpoint_append_requires_owner_identity(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    with pytest.raises(TypeError):
        journal.append_checkpoint("run", {"v": 1})  # type: ignore[call-arg]
    with pytest.raises(EffectJournalError, match="owner"):
        journal.append_checkpoint("run", {"v": 1}, worker_id="nobody", owner_epoch=1)


def test_stale_owner_outcome_cannot_write_checkpoint(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    first = AuthenticatedWorkerBinding(journal, "run", "w", 1, "s")
    journaled_effect(
        first, operation_id="op:1", idempotency_key="k1", request=1,
        invoke=lambda: 1, receipt_key="r1", checkpoint_state={"v": "owner1"},
    )
    pending = first.binding().begin_request(
        operation_id="op:2", idempotency_key="k2", request_digest="x" * 64,
    )
    journal.claim_owner("run", "w", 2)  # newer owner, before recover() runs
    with pytest.raises(EffectJournalError, match="stale worker owner epoch"):
        journal.outcome_and_checkpoint(
            EffectOutcome(pending.intent_id, EffectState.COMPLETED, "d" * 64, "r2",
                          worker_id="w", owner_epoch=1),
            {"v": "STALE"},
        )
    assert journal.get(pending.intent_id).state is EffectState.INTENT


def test_replayed_terminal_outcome_does_not_append_checkpoint_generation(tmp_path):
    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    first = AuthenticatedWorkerBinding(journal, "run", "w", 1, "s")
    intent = first.binding().begin_request(
        operation_id="op:1", idempotency_key="k1", request_digest="x" * 64,
    )
    outcome = EffectOutcome(
        intent.intent_id, EffectState.COMPLETED, "d" * 64, "r1", worker_id="w", owner_epoch=1,
    )
    journal.outcome_and_checkpoint(outcome, {"v": "first"})
    journal.outcome_and_checkpoint(outcome, {"v": "replayed"})
    restored = journal.restore_checkpoint("run")
    assert restored["generation"] == 0
    assert restored["state"] == {"v": "first"}


# --- P3 -------------------------------------------------------------------

def test_checkpoint_generations_are_pruned_to_a_bounded_window(tmp_path):
    from sonder_runtime.adapters.persistence.sqlite import effect_journal as module

    journal = SQLiteEffectJournal(tmp_path / "effects.db")
    context = AuthenticatedWorkerBinding(journal, "run", "w", 1, "s")
    total = module.CHECKPOINT_RETENTION + 5
    for index in range(total):
        journaled_effect(
            context, operation_id=f"op:{index}", idempotency_key=f"k{index}",
            request=index, invoke=lambda: 1, receipt_key=f"r{index}",
        )
    with sqlite3.connect(tmp_path / "effects.db") as connection:
        rows = connection.execute(
            "SELECT MIN(generation), MAX(generation), COUNT(*) FROM effect_checkpoint "
            "WHERE run_id='run'"
        ).fetchone()
    assert rows == (total - module.CHECKPOINT_RETENTION, total - 1, module.CHECKPOINT_RETENTION)
    assert journal.restore_checkpoint("run")["generation"] == total - 1


def test_effect_journal_protocol_append_checkpoint_matches_implementation():
    import inspect

    from sonder_runtime.application.execution.effect_journal import EffectJournal

    protocol = inspect.signature(EffectJournal.append_checkpoint)
    implementation = inspect.signature(SQLiteEffectJournal.append_checkpoint)
    assert list(protocol.parameters) == list(implementation.parameters)
    assert protocol.parameters["state"].annotation == "object"


class _AttachedRegistry:
    def __init__(self, status, *, launch_state="attached", process_id=4242):
        self.status = status
        self.launch_state = launch_state
        self.process_id = process_id

    def view(self, job_id):
        record = JobRecord(JobIdentity(job_id, "process", "launch", job_id), self.status, revision=6)
        return type("View", (), {
            "record": record,
            "process_id": self.process_id,
            "metadata": {"process_request_digest": "a" * 64, "launch_state": self.launch_state},
        })()


def _uncertain_process_start(journal, run):
    old = AuthenticatedWorkerBinding(journal, run, "process", 1, "/w")
    intent = old.binding().begin_request(
        operation_id="process-start:job-1", idempotency_key="job-1",
        request_digest="a" * 64, reconciliation="idempotent",
    )
    old.binding().mark_uncertain(intent, detail="crash after launch")
    with pytest.raises(EffectJournalError):
        AuthenticatedWorkerBinding(journal, run, "process", 2, "/w").recover_before_restart()
    return intent


@pytest.mark.parametrize("status", [JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED])
def test_process_verifier_reports_launched_start_as_completed_with_normal_receipt(tmp_path, status):
    registry = _AttachedRegistry(status)
    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={"process-start": DurableProcessEffectVerifier(lambda: registry)},
    )
    intent = _uncertain_process_start(journal, "run")
    resolved = journal.reconcile(intent.intent_id, owner_epoch=2)
    # The start effect happened; the job's later exit status is not the
    # start effect's outcome.
    assert resolved.state is EffectState.COMPLETED
    assert resolved.receipt_key == "job-1:4242"  # same shape as the live path


@pytest.mark.parametrize("launch_state,process_id", [("reserved", None), ("attached", None), ("reserved", 4242)])
def test_process_verifier_gives_no_proof_without_attached_process(tmp_path, launch_state, process_id):
    registry = _AttachedRegistry(JobStatus.FAILED, launch_state=launch_state, process_id=process_id)
    journal = SQLiteEffectJournal(
        tmp_path / "effects.db",
        reconciliation_verifiers={"process-start": DurableProcessEffectVerifier(lambda: registry)},
    )
    intent = _uncertain_process_start(journal, "run")
    with pytest.raises(EffectJournalError, match="no trusted proof"):
        journal.reconcile(intent.intent_id, owner_epoch=2)
    assert journal.get(intent.intent_id).state is EffectState.UNCERTAIN


def test_process_verifier_against_real_registry_attach_record():
    from sonder_runtime.adapters.execution.process_jobs import SubprocessJobProvider
    from sonder_runtime.application.execution.effect_journal import EffectIntent
    from sonder_runtime.application.execution.worker_bindings import _digest
    from sonder_runtime.application.jobs.durable_registry import DurableJobRegistry
    from tests.test_job004_process_provider import _Cleanup, _MemoryLimiter, _Process, _request

    registry = DurableJobRegistry()
    provider = SubprocessJobProvider(
        registry, process_cleanup=_Cleanup(complete=True),
        launcher=lambda *_a, **_k: _Process(), memory_limiter=_MemoryLimiter(),
        process_identity_resolver=lambda _pid: "stable", platform_name="posix",
    )
    request = _request("real-attach")
    provider.start(request)
    provider.wait("real-attach")
    intent = EffectIntent(
        "run:process-start:real-attach", "run", "process", "process-start:real-attach",
        "process-jobs", 1, request.identity.idempotency_key, _digest(request), "idempotent",
    )
    proof = DurableProcessEffectVerifier(lambda: registry).verify(intent)
    assert proof is not None
    assert proof.state is EffectState.COMPLETED
    assert proof.receipt_key == f"real-attach:{_Process.pid}"
