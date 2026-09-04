"""A resumed worker must have a committed running state before execution."""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, Event

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import (
    InvalidSubagentRequest, SubagentBudget, SubagentError, SubagentRequest,
    SubagentResult, SubagentStatus, SubagentUsage,
)
from sonder_runtime.application.subagents.continuable import ContinuableCheckpoint
from sonder_runtime.application.subagents.durable_continuation import (
    ChildSessionLineage, DurableChildSession, DurableContinuationService,
)


def _context():
    return local_owner_context(correlation_id="resume-state-fixture")


def _request(child_id="child-1"):
    return SubagentRequest("parent", "fixture task", SubagentBudget(max_steps=8), child_id)


def _seed(repository, *, status=SubagentStatus.FAILED, recovery=True, cancelling=False):
    result = None
    if status in {SubagentStatus.FAILED, SubagentStatus.TIMED_OUT, SubagentStatus.CANCELLED}:
        result = SubagentResult("child-1", "parent", status,
                                error=SubagentError("fixture", "previous failure", True))
    elif status is SubagentStatus.SUCCEEDED:
        result = SubagentResult("child-1", "parent", status, output="previous success")
    return repository.create(DurableChildSession(
        _request(), ChildSessionLineage("parent", ("root",)), status,
        checkpoint=ContinuableCheckpoint("child-1", 1, {"step": 2}, "next"),
        revision=7, usage=SubagentUsage(steps=2, output_tokens=13, wall_seconds=0.5),
        result=result, recovery_required=recovery, cancellation_requested=cancelling,
        cancellation_reason="fixture cancellation" if cancelling else None,
    ))


@pytest.mark.parametrize("initial_error", [RuntimeError("fixture failure"), TimeoutError("fixture timeout")])
@pytest.mark.parametrize("cancel", [False, True])
def test_resumed_worker_is_running_without_stale_result_and_accepts_cancellation(tmp_path, initial_error, cancel):
    database = tmp_path / "children.db"
    repository = SQLiteDurableContinuationRepository(database)
    initial = DurableContinuationService(repository)

    def interrupted(state, save, control):
        save({"step": 1}, "next")
        raise initial_error

    previous = initial.spawn(_request(), _context(), interrupted).result(3)
    assert previous.status in {SubagentStatus.FAILED, SubagentStatus.TIMED_OUT}
    assert initial.close(1)
    before = repository.get("child-1")
    resumed = DurableContinuationService(SQLiteDurableContinuationRepository(database))
    entered, release = Event(), Event()
    seen = []

    def runner(state, save, control):
        seen.append(dict(state))
        entered.set()
        if not release.wait(5):
            raise TimeoutError("fixture runner release timed out")
        return "fresh resumed output"

    handle = resumed.resume("child-1", _context(), runner)
    try:
        assert entered.wait(3)
        active = repository.get("child-1")
        assert handle.snapshot().status is SubagentStatus.RUNNING
        assert active.status is SubagentStatus.RUNNING
        assert active.revision == before.revision + 1
        assert not active.recovery_required
        assert active.result is None
        assert (active.request, active.lineage, active.checkpoint, active.usage) == (
            before.request, before.lineage, before.checkpoint, before.usage,
        )
        assert seen == [{"step": 1}]
        with pytest.raises(TimeoutError):
            handle.result(timeout=0)
        if cancel:
            assert handle.cancel(reason="stop resumed child")
            assert repository.get("child-1").cancellation_requested
    finally:
        release.set()
        completed = handle.result(3)
        assert resumed.close(1)
    if cancel:
        assert completed.status is SubagentStatus.CANCELLED
        assert completed.error.message == "stop resumed child"
    else:
        assert completed.status is SubagentStatus.SUCCEEDED
        assert completed.output == "fresh resumed output"
    assert completed != previous


@pytest.mark.parametrize("status", [SubagentStatus.FAILED, SubagentStatus.TIMED_OUT])
def test_resume_claim_preserves_checkpoint_usage_budget_and_lineage(tmp_path, status):
    database = tmp_path / "children.db"
    repository = SQLiteDurableContinuationRepository(database)
    before = _seed(repository, status=status)
    claimed = repository.claim_resume("child-1", expected_revision=7)
    assert claimed is not None
    assert claimed.status is SubagentStatus.RUNNING
    assert claimed.revision == 8
    assert claimed.result is None
    assert not claimed.recovery_required
    assert (claimed.request, claimed.lineage, claimed.checkpoint, claimed.usage) == (
        before.request, before.lineage, before.checkpoint, before.usage,
    )
    assert SQLiteDurableContinuationRepository(database).get("child-1") == claimed
    assert repository.claim_resume("child-1", expected_revision=8) is None


@pytest.mark.parametrize("status,recovery,cancelling", [
    (SubagentStatus.FAILED, False, False),
    (SubagentStatus.TIMED_OUT, False, False),
    (SubagentStatus.SUCCEEDED, True, False),
    (SubagentStatus.CANCELLED, True, False),
    (SubagentStatus.FAILED, True, True),
    (SubagentStatus.RUNNING, True, False),
    (SubagentStatus.CREATED, True, False),
])
def test_ineligible_resume_never_invokes_runner_or_changes_record(tmp_path, status, recovery, cancelling):
    repository = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    before = _seed(repository, status=status, recovery=recovery, cancelling=cancelling)
    calls = []
    service = DurableContinuationService(repository)
    try:
        with pytest.raises(InvalidSubagentRequest):
            service.resume("child-1", _context(), lambda *_: calls.append("called") or "output")
        assert calls == []
        assert repository.claim_resume("child-1", expected_revision=7) is None
        assert repository.get("child-1") == before
    finally:
        service.close(1)


def test_stale_or_missing_resume_claim_changes_nothing(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    before = _seed(repository)
    assert repository.claim_resume("child-1", expected_revision=6) is None
    assert repository.claim_resume("missing", expected_revision=0) is None
    assert repository.get("child-1") == before


def test_two_services_claim_one_resumed_worker(tmp_path):
    database = tmp_path / "children.db"
    seed_repository = SQLiteDurableContinuationRepository(database)
    _seed(seed_repository)
    readers = Barrier(2)

    class SynchronizedRepository(SQLiteDurableContinuationRepository):
        def __init__(self, path):
            super().__init__(path)
            self.first_read = True

        def get(self, child_id):
            record = super().get(child_id)
            if self.first_read:
                self.first_read = False
                readers.wait(timeout=3)
            return record

    services = [DurableContinuationService(SynchronizedRepository(database)) for _ in range(2)]
    entered, release = Event(), Event()
    calls = []

    def runner(state, save, control):
        calls.append(dict(state))
        entered.set()
        if not release.wait(5):
            raise TimeoutError("fixture runner release timed out")
        return "one winner"

    outcomes = []
    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(service.resume, "child-1", _context(), runner) for service in services]
            for future in futures:
                try:
                    outcomes.append(future.result(timeout=3))
                except RuntimeError as error:
                    outcomes.append(error)
        assert entered.wait(3)
        handles = [value for value in outcomes if not isinstance(value, Exception)]
        errors = [value for value in outcomes if isinstance(value, Exception)]
        assert len(handles) == 1
        assert len(errors) == 1
        assert calls == [{"step": 2}]
        assert seed_repository.get("child-1").status is SubagentStatus.RUNNING
    finally:
        release.set()
        for service in services:
            assert service.close(2)


def test_generic_update_cannot_resurrect_terminal_child(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "children.db")
    before = _seed(repository)
    assert repository.update("child-1", status=SubagentStatus.RUNNING,
                             expected_revision=7, recovery_required=False) is None
    assert repository.get("child-1") == before


def test_spawn_does_not_launch_when_repository_returns_an_unchanged_record(tmp_path):
    class NoTransitionRepository(SQLiteDurableContinuationRepository):
        def update(self, child_id, **kwargs):
            return self.get(child_id)

    repository = NoTransitionRepository(tmp_path / "children.db")
    service = DurableContinuationService(repository)
    calls = []
    try:
        with pytest.raises(RuntimeError, match="state changed before launch"):
            service.spawn(_request(), _context(), lambda *_: calls.append("called") or "output")
        assert calls == []
        assert repository.get("child-1").status is SubagentStatus.CREATED
    finally:
        service.close(1)


@pytest.mark.parametrize("resuming", [False, True])
def test_cancellation_committed_after_claim_prevents_runner_entry(tmp_path, resuming):
    class CancelAfterClaimRepository(SQLiteDurableContinuationRepository):
        def claim_resume(self, child_id, *, expected_revision):
            claimed = super().claim_resume(child_id, expected_revision=expected_revision)
            if claimed is not None:
                assert self.request_cancel(child_id, reason="cancelled before runner entry")
            return claimed

        def update(self, child_id, **kwargs):
            updated = super().update(child_id, **kwargs)
            if updated is not None and kwargs["status"] is SubagentStatus.RUNNING:
                assert self.request_cancel(child_id, reason="cancelled before runner entry")
            return updated

    repository = CancelAfterClaimRepository(tmp_path / "children.db")
    if resuming:
        _seed(repository)
    service = DurableContinuationService(repository)
    calls = []
    runner = lambda *_: calls.append("called") or "must not execute"
    try:
        handle = (service.resume("child-1", _context(), runner) if resuming
                  else service.spawn(_request(), _context(), runner))
        result = handle.result(3)
        assert calls == []
        assert result.status is SubagentStatus.CANCELLED
        assert result.error.message == "cancelled before runner entry"
    finally:
        assert service.close(1)
