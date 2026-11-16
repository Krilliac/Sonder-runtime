import sqlite3
from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic

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


# A worker that dies of an unexpected exception can still leave storage that
# looks fail-closed.  These tests require the service's own failure path.
_worker_must_not_crash = pytest.mark.filterwarnings(
    "error::pytest.PytestUnhandledThreadExceptionWarning"
)


@_worker_must_not_crash
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
    go = Event()

    def external_cancel():
        if go.wait(5):
            external_results.append(
                external.request_cancel("child-cancel-gap", reason="later stop")
            )

    # Started now, outside the service's settlement grace; it only waits.
    writer = Thread(target=external_cancel)
    writer.start()

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
            go.set()
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

    try:
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
        # One refused write, then one retry after the receipt: the wait polls
        # read-only instead of re-entering the writer lock.
        assert repository.cancel_attempts == 2
        # Nothing is left to reconcile, and the retry reused one operation
        # identity rather than leaving an orphaned cancellation-result intent.
        assert restored.unresolved_mutation("child-cancel-gap") is None
        assert sum(_is_cancelled_result(intent) for intent in intents) == 1
    finally:
        # The writer was started above, so joining it cannot raise.
        go.set()
        external.release.set()
        writer.join(5)
        assert service.close(1)


def _cancellation_runner(started):
    def wait_for_cancel(_state, _checkpoint, cancel):
        started.set()
        while not cancel.cancelled:
            cancel.wait(.01)
        return "must not publish"

    return wait_for_cancel


class _Watchdog(Exception):
    """Ends a worker that would otherwise wait past any settlement grace."""


@_worker_must_not_crash
def test_cancellation_fails_closed_when_gap_predecessor_never_receipts(tmp_path):
    """A crashed writer's intent planted in the gap is not waited on forever.

    The worker polls only for the bounded settlement grace (1.0 s), then
    publishes the ambiguity naming the orphan, and never applies its result.
    """
    path = tmp_path / "cancel-orphan.sqlite"
    child_id = "child-cancel-orphan"
    orphan = prepare_call("request_cancel", child_id, reason="crashed writer")
    started = Event()

    class OrphanGapRepository(SQLiteDurableContinuationRepository):
        def __init__(self, db_path):
            self.cancel_attempts = 0
            self.first_attempt_at = None
            super().__init__(db_path)

        def mutate(self, prepared):
            if not _is_cancelled_result(prepared):
                return super().mutate(prepared)
            self.cancel_attempts += 1
            if self.cancel_attempts == 1:
                # After the service's settlement check: plant an intent with
                # no receipt and no live writer, as a crashed process leaves.
                self.first_attempt_at = monotonic()
                connection = sqlite3.connect(path)
                try:
                    with connection:
                        connection.execute(
                            "INSERT INTO continuation_intent(operation_id,child_id,kind,digest,payload)"
                            " VALUES(?,?,?,?,?)",
                            (orphan.operation_id, orphan.child_id, orphan.kind,
                             orphan.request_sha256, orphan.payload),
                        )
                finally:
                    connection.close()
            return super().mutate(prepared)

        def reconcile(self, prepared):
            # Bound the test if the deadline were ever lost: far past the
            # grace, end the worker instead of hanging the run.
            if self.first_attempt_at is not None and monotonic() - self.first_attempt_at > 5:
                raise _Watchdog("settlement wait outlived its grace")
            return super().reconcile(prepared)

    repository = OrphanGapRepository(path)
    service = DurableContinuationService(repository)
    try:
        handle = service.spawn(_request(child_id), _context("cancel-orphan"), _cancellation_runner(started))
        assert started.wait(1)
        assert service.cancel(child_id, reason="operator stop")
        with pytest.raises(ContinuationCommitAmbiguous) as failure:
            handle.result(10)
        elapsed = monotonic() - repository.first_attempt_at
        assert failure.value.prepared.operation_id == orphan.operation_id
        # It waited out the grace (no immediate give-up), and no longer.
        assert 0.5 <= elapsed < 3.0
        # The orphan blocks every write; the worker entered mutate once.
        assert repository.cancel_attempts == 1
        stored = SQLiteDurableContinuationRepository(path).get(child_id)
        assert stored and stored.status is SubagentStatus.RUNNING and stored.result is None
    finally:
        service.close(1)


@_worker_must_not_crash
def test_cancellation_does_not_retry_its_own_ambiguous_write(tmp_path):
    """An unknown outcome of the worker's own write is never retried here.

    The write commits and its acknowledgement is lost.  The contract is that
    the caller reconciles that identity: the service surfaces the ambiguity
    after exactly one attempt instead of writing again.
    """
    path = tmp_path / "cancel-own.sqlite"
    child_id = "child-cancel-own"
    started = Event()

    class LostAckRepository(SQLiteDurableContinuationRepository):
        def __init__(self, db_path):
            self.cancel_attempts = 0
            super().__init__(db_path)

        def mutate(self, prepared):
            outcome = super().mutate(prepared)
            if _is_cancelled_result(prepared):
                self.cancel_attempts += 1
                if self.cancel_attempts > 3:
                    # Bound the test if the service ever retried its own
                    # write: each replay returns a receipt, so it would loop.
                    raise _Watchdog("own ambiguous write was retried")
                raise ContinuationCommitAmbiguous(prepared)
            return outcome

    repository = LostAckRepository(path)
    service = DurableContinuationService(repository)
    try:
        handle = service.spawn(_request(child_id), _context("cancel-own"), _cancellation_runner(started))
        assert started.wait(1)
        assert service.cancel(child_id, reason="operator stop")
        with pytest.raises(ContinuationCommitAmbiguous) as failure:
            handle.result(5)
        assert repository.cancel_attempts == 1
        own = failure.value.prepared
        assert _is_cancelled_result(own) and own.child_id == child_id
        # The committed outcome stays recoverable by reconciling that identity.
        reopened = SQLiteDurableContinuationRepository(path)
        receipt = reopened.reconcile(reopened.read_mutation(own.operation_id))
        assert receipt is not None and receipt.value.status is SubagentStatus.CANCELLED
    finally:
        service.close(1)


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
