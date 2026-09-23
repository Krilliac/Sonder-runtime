from threading import Event

import pytest

from sonder_runtime.adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.continuation_records import ChildSessionLineage, DurableChildSession
from sonder_runtime.application.ports.subagents import InvalidSubagentRequest, SubagentBudget, SubagentRequest, SubagentStatus
from sonder_runtime.application.subagents.durable_continuation import DurableContinuationService


def request(child="child-1"):
    return SubagentRequest("parent", "task", SubagentBudget(max_steps=3), child, (), "delegation-1", "delegation-1")


def test_repeated_live_delegation_reuses_the_existing_in_process_handle(tmp_path):
    repository = SQLiteDurableContinuationRepository(tmp_path / "reuse.sqlite")
    service = DurableContinuationService(repository)
    started, release = Event(), Event()
    calls = []

    def runner(*_):
        calls.append("run")
        started.set()
        release.wait(2)
        return "done"

    first = service.spawn(request(), local_owner_context(correlation_id="one"), runner)
    assert started.wait(1)
    changed_task = SubagentRequest(
        "parent", "different task", SubagentBudget(max_steps=3), "child-1", (),
        "delegation-1", "delegation-1",
    )
    with pytest.raises(InvalidSubagentRequest, match="identity or scope"):
        service.spawn(changed_task, local_owner_context(correlation_id="changed"), runner)
    second = service.spawn(request(), local_owner_context(correlation_id="two"), runner)
    assert second.child_id == first.child_id
    assert second.parent_id == first.parent_id
    release.set()
    assert first.result(2).output == "done"
    assert second.result(2).output == "done"
    assert calls == ["run"]
    service.close(1)


def test_restart_with_active_durable_child_requires_explicit_recovery(tmp_path):
    path = tmp_path / "restart-reuse.sqlite"
    repository = SQLiteDurableContinuationRepository(path)
    repository.create(DurableChildSession(request(), ChildSessionLineage("parent")))
    repository.update("child-1", status=SubagentStatus.RUNNING)
    restarted = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    with pytest.raises(InvalidSubagentRequest, match="recover/resume"):
        restarted.spawn(request(), local_owner_context(correlation_id="restart"), lambda *_: "must not run")
