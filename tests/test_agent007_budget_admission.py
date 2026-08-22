from threading import Event
import pytest
from sonder_runtime.adapters.persistence.durable_continuation import SQLiteDurableContinuationRepository
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.subagents import InvalidSubagentRequest, SubagentBudget, SubagentRequest, SubagentStatus
from sonder_runtime.application.subagents.durable_continuation import DurableContinuationService

def _ctx(name="agent007"):
    return local_owner_context(correlation_id=name)

def _request(child, parent="root", **limits):
    return SubagentRequest(parent, "bounded orchestration", SubagentBudget(**limits), child, (("role", "editor"),))

def test_role_budget_rejects_before_durable_publication(tmp_path):
    repo = SQLiteDurableContinuationRepository(tmp_path / "role.sqlite")
    with pytest.raises(InvalidSubagentRequest, match="role budget"):
        DurableContinuationService(repo).spawn(_request("child", max_steps=21, max_output_tokens=6000, max_wall_seconds=600), _ctx(), lambda *_: "ok")
    assert repo.list_all() == ()

def test_depth_and_direct_child_count_fail_closed(tmp_path):
    repo = SQLiteDurableContinuationRepository(tmp_path / "depth.sqlite")
    service = DurableContinuationService(repo)
    limits = dict(max_depth=2, max_children=1, max_concurrency=2, max_steps=20, max_output_tokens=6000, max_wall_seconds=600)
    assert service.spawn(_request("parent", **limits), _ctx(), lambda *_: "ok").result(2).status is SubagentStatus.SUCCEEDED
    assert service.spawn(_request("child", "parent", **limits), _ctx(), lambda *_: "ok").result(2).status is SubagentStatus.SUCCEEDED
    with pytest.raises(InvalidSubagentRequest, match="depth"):
        service.spawn(_request("grandchild", "child", **limits), _ctx(), lambda *_: "ok")
    with pytest.raises(InvalidSubagentRequest, match="child-count"):
        service.spawn(_request("sibling", "parent", **limits), _ctx(), lambda *_: "ok")

def test_concurrency_reservation_is_released(tmp_path):
    repo = SQLiteDurableContinuationRepository(tmp_path / "concurrency.sqlite")
    service = DurableContinuationService(repo)
    started, release = Event(), Event()
    def waiting(*_):
        started.set(); release.wait(2); return "ok"
    limits = dict(max_concurrency=1, max_steps=20, max_output_tokens=6000, max_wall_seconds=600)
    first = service.spawn(_request("first", **limits), _ctx(), waiting)
    assert started.wait(1)
    with pytest.raises(InvalidSubagentRequest, match="concurrency"):
        service.spawn(_request("second", **limits), _ctx(), waiting)
    release.set()
    assert first.result(2).status is SubagentStatus.SUCCEEDED
    assert service.spawn(_request("third", **limits), _ctx(), lambda *_: "ok").result(2).status is SubagentStatus.SUCCEEDED

def test_budget_fields_round_trip_with_lineage(tmp_path):
    path = tmp_path / "roundtrip.sqlite"
    request = _request("child", max_children=2, max_depth=3, max_concurrency=2, max_steps=20, max_output_tokens=6000, max_wall_seconds=600)
    service = DurableContinuationService(SQLiteDurableContinuationRepository(path))
    assert service.spawn(request, _ctx(), lambda *_: "ok").result(2).status is SubagentStatus.SUCCEEDED
    restored = SQLiteDurableContinuationRepository(path).get("child")
    assert restored is not None and restored.request.budget == request.budget and restored.lineage.chain == ("root",)
