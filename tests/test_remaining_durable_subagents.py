from contextlib import contextmanager
from threading import Event, Thread

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.continuation_mutations import (
    ContinuationCommitAmbiguous,
    ContinuationMutationOutcome,
    prepare_call,
)
from sonder_runtime.application.ports.subagents import SubagentBudget, SubagentRequest, SubagentStatus
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint
from sonder_runtime.application.subagents.durable_continuation import (
    ChildSessionLineage, DurableChildSession, DurableContinuationService,
)
from sonder_runtime.adapters.persistence.durable_continuation import (
    SQLiteDurableContinuationRepository,
)


def _request(child_id: str = "child-1", parent_id: str = "session-parent") -> SubagentRequest:
    return SubagentRequest(parent_id, "continue the bounded task", SubagentBudget(max_steps=8), child_id)


def _context(correlation_id: str) :
    return local_owner_context(correlation_id=correlation_id)


def test_sqlite_repository_persists_lineage_and_checkpoint_cas(tmp_path):
    path = tmp_path / "continuation.sqlite"
    first = SQLiteDurableContinuationRepository(path)
    first.create(DurableChildSession(_request(), ChildSessionLineage("session-parent")))
    saved = first.save_checkpoint(ContinuableCheckpoint("child-1", 0, {"step": 1}, "cursor-1"), expected_sequence=-1)
    assert saved is not None and saved.checkpoint is not None
    assert first.save_checkpoint(ContinuableCheckpoint("child-1", 1, {"step": 2}), expected_sequence=-1) is None

    second = SQLiteDurableContinuationRepository(path)
    restored = second.get("child-1")
    assert restored is not None
    assert restored.lineage.chain == ("session-parent",)
    assert restored.checkpoint and restored.checkpoint.state == {"step": 1}
    assert restored.checkpoint.cursor == "cursor-1"


def test_resume_after_new_service_instance_uses_durable_checkpoint(tmp_path):
    path = tmp_path / "restart.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    first = DurableContinuationService(repository)
    attempts = []

    def interrupted(state, checkpoint, _cancel):
        attempts.append(dict(state))
        if not state:
            checkpoint({"completed": "part-one"}, "part-one")
            raise RuntimeError("host stopped")
        return "resumed-result"

    assert first.spawn(_request(), _context("first"), interrupted).result(2).status is SubagentStatus.FAILED
    assert first.close(1)
    second = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    resumed = second.resume("child-1", _context("second"), interrupted)
    assert resumed.result(2).output == "resumed-result"
    assert attempts == [{}, {"completed": "part-one"}]
    second.close(1)


def test_parent_child_lineage_is_durable_and_cycle_free(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "lineage.sqlite")
    service = DurableContinuationService(repository)
    parent = service.spawn(_request("child-parent"), _context("parent"), lambda *_: "parent").result(2)
    assert parent.status is SubagentStatus.SUCCEEDED
    child = service.spawn(_request("child-child", "child-parent"), _context("child"), lambda *_: "child").result(2)
    assert child.status is SubagentStatus.SUCCEEDED
    stored = repository.get("child-child")
    assert stored is not None
    assert stored.lineage.chain == ("session-parent", "child-parent")
    service.close(1)


def test_durable_cancellation_survives_service_boundary_and_first_reason_wins(tmp_path):
    path = tmp_path / "cancel.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    service = DurableContinuationService(repository)
    started = Event()

    def wait_for_cancel(_state, _checkpoint, cancel):
        started.set()
        while not cancel.cancelled:
            cancel.wait(.01)
        return "must not publish"

    handle = service.spawn(_request("child-cancel"), _context("cancel"), wait_for_cancel)
    assert started.wait(1)
    assert service.cancel("child-cancel", reason="operator stop")
    assert not SQLiteDurableContinuationRepository(path).request_cancel("child-cancel", reason="later stop")
    assert handle.result(2).status is SubagentStatus.CANCELLED
    restored = SQLiteDurableContinuationRepository(path).get("child-cancel")
    assert restored and restored.cancellation_reason == "operator stop"
    service.close(1)


def test_public_cancellation_settles_a_prior_worker_receipt_before_retry(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "cancel-retry.sqlite")
    pending = prepare_call(
        "update",
        "child-pending",
        status=SubagentStatus.RUNNING,
        expected_revision=0,
    )
    attempts = []

    def mutate(prepared):
        attempts.append(prepared)
        if len(attempts) == 1:
            raise ContinuationCommitAmbiguous(pending)
        return ContinuationMutationOutcome("no_change", b"false", None)

    repository.mutate = mutate
    repository.reconcile = lambda _prepared: None

    assert repository.request_cancel("child-pending", reason="operator stop") is False
    assert len(attempts) == 2
    assert attempts[0] == attempts[1]


def test_cancellation_waits_for_external_intent_receipt(tmp_path):
    """A healthy writer may be between intent retention and its receipt."""
    path = tmp_path / "cancel-race.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    service = DurableContinuationService(repository)
    started = Event()
    cancel_seen = Event()
    allow_finish = Event()

    def wait_for_cancel(_state, _checkpoint, cancel):
        started.set()
        while not cancel.cancelled:
            cancel.wait(.01)
        cancel_seen.set()
        assert allow_finish.wait(3)
        return "must not publish"

    class PausedRepository(SQLiteDurableContinuationRepository):
        def __init__(self, db_path):
            self.ready = Event()
            self.release = Event()
            self.enabled = False
            self.calls = 0
            super().__init__(db_path)

        @contextmanager
        def _connect(self):
            with super()._connect() as connection:
                yield connection
            if self.enabled:
                self.calls += 1
                if self.calls == 1:
                    self.ready.set()
                    assert self.release.wait(3)

    handle = service.spawn(
        _request("child-cancel-race"), _context("cancel-race"), wait_for_cancel
    )
    assert started.wait(1)
    assert service.cancel("child-cancel-race", reason="operator stop")
    external = PausedRepository(path)
    external.enabled = True
    external_done = Event()

    def external_cancel():
        assert not external.request_cancel("child-cancel-race", reason="later stop")
        external_done.set()

    writer = Thread(target=external_cancel)
    writer.start()
    assert external.ready.wait(1)
    assert cancel_seen.wait(1)
    allow_finish.set()
    external.release.set()
    assert external_done.wait(3)
    assert handle.result(3).status is SubagentStatus.CANCELLED
    assert SQLiteDurableContinuationRepository(path).get(
        "child-cancel-race"
    ).cancellation_reason == "operator stop"
    writer.join(1)
    assert not writer.is_alive()
    service.close(1)


def _is_cancelled_result(prepared) -> bool:
    return prepared.kind == "update" and b'"status":"cancelled"' in prepared.payload


def test_cancellation_waits_for_external_intent_retained_after_settlement_check(tmp_path):
    """Regression for the CI flake of the service-boundary cancellation test.

    The worker's settlement check runs before its own intent is retained.
    Another process's ``request_cancel`` can retain its intent in exactly that
    gap and still be finishing its receipt when the worker's effect
    transaction takes the writer lock.  The earlier intent is a healthy,
    ordered predecessor: the worker must wait for its receipt and then apply
    its own retained cancellation result, not publish a storage ambiguity.
    The interleaving is forced, so this does not depend on thread scheduling.
    """
    path = tmp_path / "cancel-gap.sqlite"
    started = Event()

    class PausedRepository(SQLiteDurableContinuationRepository):
        """Pauses after its intent transaction, before its effect receipt."""

        def __init__(self, db_path):
            self.ready = Event()
            self.release = Event()
            self.enabled = False
            self.calls = 0
            super().__init__(db_path)

        @contextmanager
        def _connect(self):
            with super()._connect() as connection:
                yield connection
            if self.enabled:
                self.calls += 1
                if self.calls == 1:
                    self.ready.set()
                    assert self.release.wait(5)

    external = PausedRepository(path)
    external.enabled = True
    external_results = []

    def external_cancel():
        external_results.append(
            external.request_cancel("child-cancel-gap", reason="later stop")
        )

    writer = Thread(target=external_cancel)

    class GapRepository(SQLiteDurableContinuationRepository):
        """Opens the external intent inside the worker's check-to-intent gap."""

        def __init__(self, db_path):
            self.first_attempt_done = Event()
            self.first_blocked_by = []
            self.cancel_attempts = 0
            super().__init__(db_path)

        def mutate(self, prepared):
            if not _is_cancelled_result(prepared):
                return super().mutate(prepared)
            self.cancel_attempts += 1
            if self.cancel_attempts > 1:
                return super().mutate(prepared)
            # The service has already run its settlement check and found
            # nothing pending.  Retain the other writer's intent now, before
            # this one, and hold it short of its receipt.
            writer.start()
            assert external.ready.wait(2)
            try:
                return super().mutate(prepared)
            except ContinuationCommitAmbiguous as error:
                self.first_blocked_by.append(error.prepared.operation_id)
                raise
            finally:
                # Only now may the other writer finish its receipt.  Released
                # here, not from the test thread, to keep the hand-off well
                # inside the service's settlement grace.
                self.first_attempt_done.set()
                external.release.set()

    repository = GapRepository(path)
    service = DurableContinuationService(repository)

    def wait_for_cancel(_state, _checkpoint, cancel):
        started.set()
        while not cancel.cancelled:
            cancel.wait(.01)
        return "must not publish"

    handle = service.spawn(_request("child-cancel-gap"), _context("cancel-gap"), wait_for_cancel)
    assert started.wait(1)
    assert service.cancel("child-cancel-gap", reason="operator stop")
    assert repository.first_attempt_done.wait(3)
    writer.join(3)
    assert not writer.is_alive()
    assert external_results == [False]  # first reason wins
    assert handle.result(3).status is SubagentStatus.CANCELLED
    restored = SQLiteDurableContinuationRepository(path)
    stored = restored.get("child-cancel-gap")
    assert stored and stored.status is SubagentStatus.CANCELLED
    assert stored.cancellation_reason == "operator stop"
    intents = [
        restored.read_mutation(operation_id)
        for _position, operation_id in restored.mutation_ids("child-cancel-gap")[0]
    ]
    later = [
        intent for intent in intents
        if intent.kind == "request_cancel" and b'"later stop"' in intent.payload
    ]
    # The forced gap really happened: the worker's first write was refused
    # because of the other writer's earlier, still-unreceipted intent.
    assert len(later) == 1 and repository.first_blocked_by == [later[0].operation_id]
    assert repository.cancel_attempts >= 2
    # Nothing is left to reconcile, and the retry reused one operation
    # identity rather than leaving an orphaned cancellation-result intent.
    assert restored.unresolved_mutation("child-cancel-gap") is None
    assert sum(_is_cancelled_result(intent) for intent in intents) == 1
    assert service.close(1)


def test_restart_recovery_requires_cleanup_and_storage_reconciliation(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "orphan.sqlite")
    repository.create(DurableChildSession(_request("child-orphan"), ChildSessionLineage("session-parent")))
    running = repository.update("child-orphan", status=SubagentStatus.RUNNING)
    assert running is not None
    service = DurableContinuationService(SQLiteDurableContinuationRepository(tmp_path / "orphan.sqlite"))
    from sonder_runtime.application.ports.continuation_mutations import ContinuationCleanupRequired
    with pytest.raises(ContinuationCleanupRequired):
        service.recover_after_restart()
    recovered = repository.get("child-orphan")
    assert recovered and recovered.status is SubagentStatus.RUNNING
    assert not recovered.recovery_required and recovered.result is None


def test_lineage_rejects_cycles():
    with pytest.raises(ValueError):
        ChildSessionLineage("parent", ("ancestor", "parent"))
