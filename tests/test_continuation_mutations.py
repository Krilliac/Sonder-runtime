from concurrent.futures import ThreadPoolExecutor
import subprocess
import sys

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)
from sonder_runtime.application.ports.continuation_mutations import (
    prepare_call,
    ContinuationCommitAmbiguous,
    ContinuationReceiptCapacity,
    ContinuationStorageFailure,
)
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint
from sonder_runtime.application.subagents.durable_continuation import (
    DurableContinuationService,
)
from tests.test_durable_child_resume_state import _seed, _request, _context


def test_prepared_payload_detaches_and_receipt_retains_original_result(tmp_path):
    repo = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    _seed(repo)
    state = {"step": 3}
    checkpoint = ContinuableCheckpoint("child-1", 2, state)
    prepared = prepare_call(
        "save_checkpoint",
        checkpoint,
        expected_sequence=1,
    )
    state["step"] = 999
    checkpoint.state["step"] = 888
    first = repo.mutate(prepared)
    repo.save_checkpoint(
        ContinuableCheckpoint("child-1", 3, {"step": 4}), expected_sequence=2
    )
    replay = repo.mutate(prepared)
    assert replay.replayed and replay.result_bytes == first.result_bytes
    assert replay.value.checkpoint.state == {"step": 3}
    assert repo.get("child-1").checkpoint.sequence == 3
    changed = prepare_call(
        "save_checkpoint",
        ContinuableCheckpoint("child-1", 2, {"step": 7}),
        expected_sequence=1,
        operation_id=prepared.operation_id,
    )
    with pytest.raises(ValueError, match="identity"):
        repo.mutate(changed)
    assert repo.reconcile(prepared).result_bytes == first.result_bytes


def test_retained_identity_pages_preserve_older_operations_and_scope(tmp_path):
    repo = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    _seed(repo)
    first = repo.latest_mutation("child-1")
    for number in range(105):
        repo.request_cancel("child-1", reason=f"request {number}")
    ids, more = repo.mutation_ids("child-1", limit=100)
    assert len(ids) == 100 and more
    remaining, more = repo.mutation_ids("child-1", after=ids[-1][0], limit=100)
    assert len(remaining) == 6 and not more
    assert len({operation for _, operation in ids + remaining}) == 106
    assert repo.read_mutation(first.operation_id) == first
    assert repo.reconcile(first).value.request.child_id == "child-1"
    assert repo.mutation_ids("another-child") == ((), False)


def test_receipt_capacity_rejects_new_write_but_preserves_replay(tmp_path):
    repo = SQLiteDurableContinuationRepository(tmp_path / "children.db", max_receipts=1)
    command = prepare_call("create", _seed_value())
    outcome = repo.mutate(command)
    with pytest.raises(ContinuationReceiptCapacity):
        repo.request_cancel("child-1", reason="stop")
    assert not repo.get("child-1").cancellation_requested
    assert repo.mutate(command).result_bytes == outcome.result_bytes


def _seed_value():
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableChildSession,
        ChildSessionLineage,
    )

    return DurableChildSession(_request(), ChildSessionLineage("parent"))


def test_independent_connections_single_resume_and_same_command_replay(tmp_path):
    path = tmp_path / "children.db"
    a, b = SQLiteDurableContinuationRepository(
        path
    ), SQLiteDurableContinuationRepository(path)
    _seed(a)
    commands = [
        prepare_call("claim_resume", "child-1", expected_revision=7) for _ in range(2)
    ]
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(
            pool.map(lambda pair: pair[0].mutate(pair[1]), zip((a, b), commands))
        )
    assert sum(result.value is not None for result in outcomes) == 1
    for repo, command, result in zip((a, b), commands, outcomes):
        assert repo.mutate(command).result_bytes == result.result_bytes


def test_ambiguous_checkpoint_stops_runner_without_retryable_failure(tmp_path):
    class Ambiguous(SQLiteDurableContinuationRepository):
        def mutate(self, prepared):
            result = super().mutate(prepared)
            if prepared.kind == "save_checkpoint":
                raise ContinuationCommitAmbiguous(prepared)
            return result

    repo = Ambiguous(tmp_path / "children.db")
    service = DurableContinuationService(repo)
    effects = []

    def runner(state, save, control):
        effects.append("before")
        save({"step": 1})
        effects.append("after")
        return "done"

    handle = service.spawn(_request(), _context(), runner)
    with pytest.raises(ContinuationCommitAmbiguous):
        handle.result(3)
    row = repo.get("child-1")
    assert effects == ["before"] and row.result is None and not row.recovery_required
    assert row.status.value == "running" and row.checkpoint.sequence == 0
    with pytest.raises(ContinuationCommitAmbiguous):
        service.recover_after_restart()


def test_ambiguous_resume_never_starts_runner(tmp_path):
    class Ambiguous(SQLiteDurableContinuationRepository):
        def mutate(self, prepared):
            result = super().mutate(prepared)
            if prepared.kind == "claim_resume":
                raise ContinuationCommitAmbiguous(prepared)
            return result

    repo = Ambiguous(tmp_path / "children.db")
    _seed(repo)
    service = DurableContinuationService(repo)
    effects = []
    with pytest.raises(ContinuationCommitAmbiguous):
        service.resume("child-1", _context(), lambda *args: effects.append("run"))
    assert effects == [] and repo.get("child-1").status.value == "running"


@pytest.mark.parametrize("after_commit", [False, True])
def test_process_exit_retains_exact_intent_and_prevents_automatic_resume(
    tmp_path, after_commit
):
    path = tmp_path / "children.db"
    repo = SQLiteDurableContinuationRepository(path)
    _seed(repo)
    script = """
import os, sys
from sonder_runtime.adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository
from sonder_runtime.application.ports.continuation_mutations import prepare_call
class Crash(SQLiteDurableContinuationRepository):
    def _apply_claim_resume(self, connection, *args, **kwargs):
        result = super()._apply_claim_resume(connection, *args, **kwargs)
        if sys.argv[2] == 'False': os._exit(73)
        return result
repo = Crash(sys.argv[1])
repo.mutate(prepare_call('claim_resume', 'child-1', expected_revision=7, operation_id='crashed-resume'))
os._exit(73)
"""
    process = subprocess.run(
        [sys.executable, "-c", script, str(path), str(after_commit)], timeout=15
    )
    assert process.returncode == 73
    reopened = SQLiteDurableContinuationRepository(path)
    prepared = reopened.read_mutation("crashed-resume")
    assert prepared.operation_id == "crashed-resume"
    assert (reopened.reconcile(prepared) is not None) is after_commit
    service = DurableContinuationService(reopened)
    effects = []
    with pytest.raises((ContinuationCommitAmbiguous, ValueError)):
        service.resume("child-1", _context(), lambda *args: effects.append("run"))
    assert effects == []
    assert reopened.get("child-1").status.value == (
        "running" if after_commit else "failed"
    )
    if after_commit:
        with pytest.raises(ContinuationCommitAmbiguous):
            service.recover_after_restart()


def test_same_command_concurrent_connections_insert_one_original_receipt(tmp_path):
    path = tmp_path / "children.db"
    repositories = [SQLiteDurableContinuationRepository(path) for _ in range(2)]
    prepared = prepare_call("create", _seed_value())
    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(lambda repo: repo.mutate(prepared), repositories))
    assert sum(not result.replayed for result in outcomes) == 1
    assert outcomes[0].result_bytes == outcomes[1].result_bytes
    ids, more = repositories[0].mutation_ids("child-1", limit=1)
    assert len(ids) == 1 and not more and ids[0][1] == prepared.operation_id


def test_byte_capacity_rolls_back_state_and_retains_unresolved_identity(tmp_path):
    path = tmp_path / "children.db"
    repository = SQLiteDurableContinuationRepository(path, max_receipt_bytes=800)
    prepared = prepare_call("create", _seed_value())
    assert len(prepared.payload) < 800 < len(prepared.payload) * 2
    with pytest.raises(ContinuationReceiptCapacity):
        repository.mutate(prepared)
    assert repository.get("child-1") is None
    assert repository.unresolved_mutation("child-1") == prepared
    assert repository.reconcile(prepared) is None
    reopened = SQLiteDurableContinuationRepository(path)
    assert reopened.mutate(prepared).value.request.child_id == "child-1"
    assert reopened.unresolved_mutation("child-1") is None


def test_read_storage_failure_does_not_become_retryable_runner_failure(tmp_path):
    import sqlite3

    repository = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    service = DurableContinuationService(repository)
    effects = []

    def runner(state, save, control):
        def unavailable(*args):
            raise sqlite3.OperationalError("fixture read unavailable")

        repository._select = unavailable
        control.cancelled
        effects.append("after")

    handle = service.spawn(_request(), _context(), runner)
    with pytest.raises(ContinuationStorageFailure):
        handle.result(3)
    reopened = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    row = reopened.get("child-1")
    assert effects == [] and row.status.value == "running" and row.result is None


def test_lost_terminal_ack_preserves_committed_result_without_new_effects(tmp_path):
    class Ambiguous(SQLiteDurableContinuationRepository):
        def mutate(self, prepared):
            outcome = super().mutate(prepared)
            if prepared.kind == "update" and outcome.value.result is not None:
                raise ContinuationCommitAmbiguous(prepared)
            return outcome

    path = tmp_path / "children.db"
    service = DurableContinuationService(Ambiguous(path))
    handle = service.spawn(_request(), _context(), lambda *args: "completed output")
    with pytest.raises(ContinuationCommitAmbiguous) as failure:
        handle.result(3)
    reopened = SQLiteDurableContinuationRepository(path)
    retained = reopened.read_mutation(failure.value.prepared.operation_id)
    assert reopened.reconcile(retained).value.result.output == "completed output"
    assert not reopened.get("child-1").recovery_required
    with pytest.raises(ContinuationCommitAmbiguous):
        service.resume("child-1", _context(), lambda *args: pytest.fail("new effect"))
    assert service.close(1)
