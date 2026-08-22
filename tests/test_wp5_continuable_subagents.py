from threading import Event

import pytest

from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import SubagentBudget, SubagentRequest, SubagentStatus
from sonder_runtime.application.subagents.continuable import (
    ContinuableCheckpoint, ContinuableSubagentService,
    InMemoryContinuableSubagentRepository,
)


def request(child_id="child-1"):
    return SubagentRequest("parent-1", "continue task", SubagentBudget(max_steps=10), child_id)


def test_checkpointed_child_resumes_from_last_durable_state():
    store = InMemoryContinuableSubagentRepository()
    service = ContinuableSubagentService(store)
    attempts = []

    def runner(state, checkpoint, _cancel):
        attempts.append(dict(state))
        if not state:
            checkpoint({"value": 1}, "step-1")
            raise RuntimeError("process interrupted")
        assert state == {"value": 1}
        return "done"

    context = local_owner_context(correlation_id="c1")
    handle = service.spawn(request(), context, runner)
    first = handle.result(2)
    assert first.status is SubagentStatus.FAILED
    assert first.error and first.error.retryable
    assert store.get("child-1").checkpoint.state == {"value": 1}
    resumed = service.resume("child-1", context, runner)
    assert resumed.result(2).output == "done"
    assert attempts == [{}, {"value": 1}]


def test_cancel_is_cooperative_and_first_reason_wins():
    store = InMemoryContinuableSubagentRepository()
    service = ContinuableSubagentService(store)
    started = Event()

    def runner(state, checkpoint, cancel):
        started.set()
        while not cancel.cancelled:
            cancel.wait(.01)
        return "must not publish"

    handle = service.spawn(request("child-2"), local_owner_context(correlation_id="c2"), runner)
    assert started.wait(1)
    assert handle.cancel(reason="user stop")
    assert not handle.cancel(reason="later stop")
    result = handle.result(2)
    assert result.status is SubagentStatus.CANCELLED
    assert handle.snapshot().cancellation_reason == "user stop"


def test_recover_marks_orphaned_running_record_retryable():
    store = InMemoryContinuableSubagentRepository()
    service = ContinuableSubagentService(store)
    service.spawn(request("child-3"), local_owner_context(correlation_id="c3"), lambda *_: "ok")
    # Simulate a process restart: the durable record says running but no worker remains.
    record = store.get("child-3")
    store.update("child-3", status=SubagentStatus.RUNNING, recovery_required=True)
    assert service.recover() == ("child-3",)
    assert store.get("child-3").result.error.code == "interrupted"
    service.close(1)


def test_checkpoint_compare_and_set_rejects_stale_writer():
    store = InMemoryContinuableSubagentRepository()
    service = ContinuableSubagentService(store)
    service.spawn(request("child-4"), local_owner_context(correlation_id="c4"), lambda *_: "ok")
    assert store.save_checkpoint(ContinuableCheckpoint("child-4", 0, {"x": 1}), expected_sequence=-1)
    assert store.save_checkpoint(ContinuableCheckpoint("child-4", 1, {"x": 2}), expected_sequence=-1) is None
    service.close(1)
