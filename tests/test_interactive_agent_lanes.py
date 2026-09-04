"""Behavior tests for durable independently conversational lanes."""

from dataclasses import replace
from threading import Event, Thread
import pytest
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)


class Model:
    def __init__(self):
        self.requests = []
        self.callback = None

    def generate(self, request, context):
        self.requests.append((request, context))
        if self.callback:
            self.callback()
        return ModelResponse("A verified result", "fake", request.tier, tokens_out=4)


@pytest.fixture
def env(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    model = Model()
    service = AgentLaneService(store, sessions, model, auto_start=False)
    context = local_owner_context(correlation_id="test", workspace_roots=(tmp_path,))
    (tmp_path / "child").mkdir()
    return service, store, sessions, model, context, tmp_path


def spawn(env, command="spawn-1"):
    service, _, _, _, context, root = env
    return service.spawn(
        command_id=command,
        parent_session_id="parent",
        task="implement parser",
        workspace_root=str(root / "child"),
        context=context,
    )


def test_spawn_is_durable_idempotent_and_real_independent_turn(env):
    service, store, sessions, model, context, _ = env
    first = spawn(env)
    assert spawn(env) == first
    lane_id = first["lane"]["id"]
    assert first["lane"]["session_id"] != "parent"
    service.run_pending(lane_id, context)
    assert len(model.requests) == 1
    assert "implement parser" in model.requests[0][0].prompt
    assert service.inspect(lane_id, context)["lane"]["status"] == "completed"
    events = sessions.read_range(first["lane"]["session_id"])
    assert any(e.event_type == "model.requested" for e in events)
    assert any(e.event_type == "model.response" for e in events)
    reopened = AgentLaneService(store, sessions, model, auto_start=False)
    assert (
        reopened.inspect(lane_id, context)["lane"]["session_id"]
        == first["lane"]["session_id"]
    )


def test_user_and_parent_steering_enter_next_request_in_order(env):
    service, _, _, model, context, _ = env
    lane = spawn(env)["lane"]["id"]

    def steer():
        model.callback = None
        service.send_message(
            lane,
            command_id="user-1",
            content="Preserve Unicode",
            author="user",
            context=context,
        )
        service.send_message(
            lane,
            command_id="parent-1",
            content="Add tests",
            author="parent",
            context=context,
        )

    model.callback = steer
    service.run_pending(lane, context)
    assert len(model.requests) == 2
    prompt = model.requests[1][0].prompt
    assert prompt.index("Preserve Unicode") < prompt.index("Add tests")
    messages = service.inspect(lane, context)["messages"]
    assert [m["author"] for m in messages] == ["parent", "user", "parent"]
    assert all(m["delivery_state"] == "handled" for m in messages)


def test_interrupt_acknowledged_only_after_inflight_call_and_resume_keeps_identity(env):
    service, _, _, model, context, _ = env
    lane = spawn(env)["lane"]["id"]

    def interrupt():
        model.callback = None
        receipt = service.control(lane, "interrupt", command_id="stop", context=context)
        assert receipt["lane"]["status"] == "interrupt_requested"

    model.callback = interrupt
    service.run_pending(lane, context)
    before = service.inspect(lane, context)["lane"]
    assert before["status"] == "interrupted"
    resumed = service.control(
        lane, "resume", command_id="resume", content="Continue", context=context
    )["lane"]
    assert resumed["session_id"] == before["session_id"]
    assert resumed["attempt_id"] != before["attempt_id"]
    service.run_pending(lane, context)
    assert len(model.requests) == 2
    assert model.requests[1][0].history


def test_completed_lane_accepts_followup_and_parent_reports_are_durable(env):
    service, store, sessions, model, context, _ = env
    lane = spawn(env)["lane"]["id"]
    service.run_pending(lane, context)
    reports = service.reports("parent", context)["reports"]
    assert len(reports) == 1 and not reports[0]["acknowledged"]
    service.send_message(
        lane,
        command_id="followup",
        content="One more thing",
        author="user",
        context=context,
    )
    service.run_pending(lane, context)
    assert len(model.requests) == 2
    assert len(service.reports("parent", context)["reports"]) == 2
    service.ack_report(reports[0]["id"], command_id="ack", context=context)
    reopened = AgentLaneService(store, sessions, model, auto_start=False)
    assert reopened.reports("parent", context)["reports"][0]["acknowledged"]


def test_scope_and_workspace_and_command_identity_cannot_expand(env):
    service, _, _, model, context, root = env
    first = spawn(env)
    outsider = replace(context, principal_id="outsider")
    with pytest.raises(PermissionError):
        service.inspect(first["lane"]["id"], outsider)
    with pytest.raises(PermissionError):
        service.spawn(
            command_id="outside",
            parent_session_id="parent",
            task="x",
            workspace_root=str(root.parent),
            context=context,
        )
    with pytest.raises(ValueError):
        service.spawn(
            command_id="spawn-1",
            parent_session_id="parent",
            task="changed",
            workspace_root=str(root / "child"),
            context=context,
        )
    with pytest.raises(ValueError):
        service.spawn(
            command_id="overlap",
            parent_session_id="parent",
            task="x",
            workspace_root=str(root / "child"),
            context=context,
        )
    assert not model.requests


def test_cancelled_lane_cannot_resume_or_run(env):
    service, _, _, model, context, _ = env
    lane = spawn(env)["lane"]["id"]
    service.control(lane, "cancel", command_id="cancel", context=context)
    with pytest.raises(ValueError):
        service.control(lane, "resume", command_id="resume", context=context)
    service.run_pending(lane, context)
    assert not model.requests


def test_uncertain_provider_effect_is_not_replayed_on_resume(env):
    service, store, sessions, model, context, _ = env
    lane = spawn(env)["lane"]["id"]

    def uncertain():
        raise OSError("connection lost after dispatch")

    model.callback = uncertain
    service.run_pending(lane, context)
    assert service.inspect(lane, context)["lane"]["status"] == "awaiting_input"
    with pytest.raises(ValueError):
        service.control(lane, "resume", command_id="resume", context=context)
    assert len(model.requests) == 1
    assert service.inspect(lane, context)["messages"][0]["delivery_state"] == "accepted"


def test_outbox_replays_projection_gap_without_duplicate_transcript(env):
    service, store, sessions, model, context, _ = env
    original = sessions.append_once
    failed = False

    def crash_after_append(*args, **kwargs):
        nonlocal failed
        result = original(*args, **kwargs)
        if not failed:
            failed = True
            raise OSError("simulated process exit after canonical append")
        return result

    sessions.append_once = crash_after_append
    with pytest.raises(OSError):
        spawn(env)
    sessions.append_once = original
    receipt = spawn(env)
    store.flush()
    events = sessions.read_range(receipt["lane"]["session_id"])
    assert [e.event_type for e in events] == ["lane.created", "lane.message"]
    assert not model.requests


def test_competing_service_claim_runs_one_model_attempt(env):
    service, store, sessions, model, context, _ = env
    lane = spawn(env)["lane"]["id"]
    entered, release = Event(), Event()

    def blocked():
        entered.set()
        assert release.wait(5)

    model.callback = blocked
    worker = Thread(target=service.run_pending, args=(lane, context))
    worker.start()
    assert entered.wait(5)
    other = AgentLaneService(store, sessions, model, auto_start=False)
    other.run_pending(lane, context)
    assert len(model.requests) == 1
    release.set()
    worker.join(5)
    assert service.inspect(lane, context)["lane"]["status"] == "completed"


def test_lifetime_step_budget_does_not_reset_after_resume(env):
    service, _, _, model, context, root = env
    receipt = service.spawn(
        command_id="s",
        parent_session_id="parent",
        task="x",
        workspace_root=str(root / "child"),
        max_steps=1,
        context=context,
    )
    lane = receipt["lane"]["id"]
    service.run_pending(lane, context)
    with pytest.raises(ValueError, match="budget"):
        service.control(lane, "resume", command_id="r", context=context)
    assert len(model.requests) == 1


def test_http_and_model_author_identity_cannot_be_spoofed(env):
    from sonder_runtime.interfaces.http.facades.agent_lanes import (
        dispatch_agent_lane_route,
    )
    from sonder_runtime.interfaces.agent_lanes import dispatch_agent_lane_tool

    service, _, _, _, context, _ = env
    lane = spawn(env)["lane"]["id"]
    response = dispatch_agent_lane_route(
        service,
        "POST",
        "/v1/agent-lanes/" + lane + "/messages",
        {"command_id": "bad", "content": "x", "author": "parent"},
        {},
        context,
    )
    assert response.status_code == 403
    response = dispatch_agent_lane_route(
        service,
        "POST",
        "/v1/agent-lanes/" + lane + "/messages",
        {"command_id": "good", "content": "user correction"},
        {},
        context,
    )
    assert response.status_code == 202
    with pytest.raises(PermissionError):
        dispatch_agent_lane_tool(
            service,
            "send_message",
            {"lane_id": lane, "command_id": "bad2", "content": "x"},
            context,
            "unrelated",
        )
    assert service.inspect(lane, context)["messages"][-1]["author"] == "user"


def test_steering_not_duplicated_or_admitted_via_history_before_safe_boundary(env):
    service, _, _, model, context, _ = env
    lane = spawn(env)["lane"]["id"]
    service.run_pending(lane, context)
    request = model.requests[0][0]
    assert request.history == ()


def test_http_spawn_is_attributed_to_user(env):
    from sonder_runtime.interfaces.http.facades.agent_lanes import (
        dispatch_agent_lane_route,
    )

    service, _, _, _, context, root = env
    result = dispatch_agent_lane_route(
        service,
        "POST",
        "/v1/agent-lanes",
        {
            "command_id": "http-spawn",
            "parent_session_id": "parent",
            "task": "user task",
            "workspace_root": str(root / "child"),
        },
        {},
        context,
    )
    assert result.status_code == 202
    assert (
        service.inspect(result.body["lane"]["id"], context)["messages"][0]["author"]
        == "user"
    )


def test_legacy_provider_can_bind_prompt_and_context_before_runner_entry(tmp_path):
    from sonder_runtime.adapters.subagents import LocalSubagentProvider
    from sonder_runtime.application.subagents.durable_continuation import (
        DurableContinuationService,
    )
    from sonder_runtime.adapters.persistence.durable_continuation import (
        SQLiteDurableContinuationRepository,
    )
    from sonder_runtime.application.ports.subagents import (
        SubagentRequest,
        SubagentBudget,
    )

    durable = DurableContinuationService(
        SQLiteDurableContinuationRepository(tmp_path / "children.db")
    )
    seen = []

    def factory(request, context):
        def runner(state, save, control):
            seen.append((request.prompt, context.principal_id))
            return "real result"

        return runner

    provider = LocalSubagentProvider(durable, runner_factory=factory)
    provider.register_root(
        "parent",
        SubagentBudget(max_children=2, max_depth=2, max_concurrency=2, max_steps=3),
    )
    handle = provider.spawn(
        SubagentRequest(
            "parent",
            "work to do",
            SubagentBudget(max_children=2, max_depth=2, max_concurrency=2, max_steps=2),
        ),
        local_owner_context(correlation_id="legacy"),
    )
    result = handle.result(5)
    assert result.output == "real result"
    assert seen == [("work to do", "owner")]


def test_tool_request_uses_scoped_typed_gateway_and_records_artifact(env):
    from sonder_runtime.application.ports.tool_registry import (
        InMemoryToolRegistry,
        ToolDescriptor,
    )
    from sonder_runtime.application.ports.tool_execution import ToolExecutionResult
    from sonder_runtime.application.tools.facade import ToolApplicationFacade
    from sonder_runtime.application.tools.resource_policy import (
        ResourcePolicy,
        PolicyRule,
        Decision,
    )
    from sonder_runtime.domain.tools.descriptors import ToolEffect

    service, _, _, model, context, root = env
    observed = []

    class Executor:
        def execute(self, descriptor, call, ctx, execution_class):
            observed.append((call, ctx))
            return ToolExecutionResult(
                tool_name=descriptor.name, success=True, output="created"
            )

    descriptor = ToolDescriptor(
        "write_file",
        input_schema={
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
        effects=frozenset({ToolEffect.WRITE_FILES}),
    )
    service.tools = ToolApplicationFacade.compose(
        InMemoryToolRegistry([descriptor]),
        Executor(),
        policy=ResourcePolicy([PolicyRule("allow", Decision.ALLOW, tool="write_file")]),
    )
    lane = spawn(env)["lane"]["id"]
    replies = iter(
        [
            '{"tool":"write_file","arguments":{"path":"result.txt","content":"ok"}}',
            "Created result.txt",
        ]
    )

    def generate(request, ctx):
        model.requests.append((request, ctx))
        return ModelResponse(next(replies), "fake", "code", tokens_out=10)

    model.generate = generate
    service.run_pending(lane, context)
    assert len(observed) == 1
    assert observed[0][0].arguments["path"] == str(
        (root / "child" / "result.txt").resolve()
    )
    assert observed[0][1].workspace_roots == (root / "child",)
    assert service.reports("parent", context)["reports"][0]["artifacts"] == [
        str(root / "child" / "result.txt")
    ]


def test_model_tool_cannot_address_parent_workspace(env):
    from types import SimpleNamespace

    service, _, _, _, context, root = env
    service.tools = SimpleNamespace(
        graph=SimpleNamespace(
            registry=SimpleNamespace(
                list_all=lambda: [SimpleNamespace(name="write_file")],
                get=lambda n: SimpleNamespace(effects=()),
            )
        )
    )
    lane_id = spawn(env)["lane"]["id"]
    with service.store.transaction() as tx:
        lane = tx.lane(lane_id)
    with pytest.raises(PermissionError):
        service._tool_call(
            '{"tool":"write_file","arguments":{"path":"../outside.txt","content":"x"}}',
            lane,
        )


def test_process_exit_preserves_admitted_attempt_and_requires_reconciliation(env):
    import subprocess
    import sys

    service, store, sessions, _, context, root = env
    lane = spawn(env)["lane"]["id"]
    script = """
import os,sys
from pathlib import Path
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.context import local_owner_context
class CrashModel:
    def generate(self,request,context):
        os._exit(17)
root=Path(sys.argv[1])
sessions=SQLiteSessionRepository(root/'sessions.db')
service=AgentLaneService(SQLiteAgentLaneStore(root/'fleet.db',sessions),sessions,CrashModel(),auto_start=False)
service.run_pending(sys.argv[2],local_owner_context(correlation_id='child',workspace_roots=(root,)))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(root), lane], timeout=15
    )
    assert completed.returncode == 17
    state = service.inspect(lane, context)
    assert state["lane"]["status"] == "awaiting_input"
    assert state["messages"][0]["delivery_state"] == "accepted"
    with pytest.raises(ValueError):
        service.control(lane, "resume", command_id="unsafe", context=context)
    types = [
        event.event_type for event in sessions.read_range(state["lane"]["session_id"])
    ]
    assert "model.requested" in types and "model.response" not in types


def test_fleet_pruning_keeps_retained_lane_mailbox(env, monkeypatch):
    from sonder_runtime.adapters.persistence import fleet_store

    service, store, _, _, context, _ = env
    monkeypatch.setenv("SONDER_FLEET_DB", store.path)
    lane = spawn(env)["lane"]["id"]
    service.run_pending(lane, context)
    with store.connect() as conn:
        conn.execute(
            "UPDATE fleet_messages SET queued_ts=1,delivered_ts=1 WHERE status='delivered'"
        )
    fleet_store.prune(message_retention_seconds=3600)
    assert service.inspect(lane, context)["messages"][0]["delivery_state"] == "handled"


def test_parent_capability_is_scoped_revocable_and_not_stored_in_clear(env):
    service, store, _, _, context, _ = env
    capability = service.open_model_parent(context)
    service.verify_model_parent(
        capability["parent_session_id"], capability["parent_token"], context
    )
    with pytest.raises(PermissionError):
        service.verify_model_parent("parent", capability["parent_token"], context)
    with pytest.raises(PermissionError):
        service.verify_model_parent(
            capability["parent_session_id"],
            capability["parent_token"],
            replace(context, principal_id="other"),
        )
    with store.connect() as conn:
        dump = "\n".join(conn.iterdump())
    assert capability["parent_token"] not in dump
    replacement = service.rotate_model_parent(
        capability["parent_session_id"], capability["parent_token"], context
    )
    with pytest.raises(PermissionError):
        service.verify_model_parent(
            capability["parent_session_id"], capability["parent_token"], context
        )
    service.revoke_model_parent(
        replacement["parent_session_id"], replacement["parent_token"], context
    )
    with pytest.raises(PermissionError):
        service.verify_model_parent(
            replacement["parent_session_id"], replacement["parent_token"], context
        )


def test_provider_exception_details_never_enter_public_lane_events(env):
    service, _, sessions, model, context, _ = env
    lane = spawn(env)["lane"]["id"]

    def failure():
        raise RuntimeError("secret-provider-body-123")

    model.callback = failure
    service.run_pending(lane, context)
    state = service.inspect(lane, context)
    assert "secret-provider-body-123" not in str(state)
    assert "secret-provider-body-123" not in str(
        sessions.read_range(state["lane"]["session_id"])
    )


def test_authority_revalidated_before_queued_model_admission(env):
    service, _, _, model, context, _ = env
    allowed = True

    def authorize(lane, ctx):
        if not allowed:
            raise PermissionError("revoked")

    service.authorize_grant = authorize
    lane = spawn(env)["lane"]["id"]
    allowed = False
    with pytest.raises(PermissionError):
        service.run_pending(lane, context)
    assert not model.requests


def test_workspace_reservation_is_global_across_authorized_principals(env):
    service, _, _, _, context, root = env
    spawn(env)
    service.authorize_grant = lambda lane, ctx: None
    with pytest.raises(ValueError, match="overlap"):
        service.spawn(
            command_id="other",
            parent_session_id="other-parent",
            task="x",
            workspace_root=str(root / "child"),
            context=replace(context, principal_id="other"),
        )


def test_fleet_metadata_does_not_contain_private_task(env):
    service, store, _, _, context, root = env
    receipt = service.spawn(
        command_id="private",
        parent_session_id="parent",
        task="private-password-reset-body",
        workspace_root=str(root / "child"),
        context=context,
    )
    with store.connect() as conn:
        row = conn.execute(
            "SELECT task FROM fleet_agents WHERE id=?", (receipt["lane"]["id"],)
        ).fetchone()
    assert "private-password-reset-body" not in row[0]


def test_wait_admission_shared_across_graphs_and_released(env):
    from sonder_runtime.application.errors import CapacityExceeded

    service, store, sessions, model, context, _ = env
    lane = spawn(env)["lane"]["id"]
    other = AgentLaneService(store, sessions, model, auto_start=False)
    release = Event()
    entered = []

    def block(*args, **kwargs):
        entered.append(True)
        assert release.wait(5)
        return {}

    service._wait_admitted = block
    threads = [Thread(target=service.wait, args=(lane, context)) for _ in range(2)]
    for thread in threads:
        thread.start()
    import time

    end = time.monotonic() + 3
    while len(entered) < 2 and time.monotonic() < end:
        time.sleep(0.01)
    assert len(entered) == 2
    with pytest.raises(CapacityExceeded):
        other.wait(lane, context, timeout_seconds=0)
    release.set()
    for thread in threads:
        thread.join(5)
    assert other.wait(lane, context, timeout_seconds=0)["lane"]["id"] == lane


def test_oversized_provider_body_is_not_persisted_even_with_small_usage(env):
    service, _, sessions, model, context, _ = env
    lane = spawn(env)["lane"]["id"]
    model.generate = lambda request, ctx: ModelResponse(
        "x" * 70000, "fake", "code", tokens_out=1
    )
    service.run_pending(lane, context)
    state = service.inspect(lane, context)
    assert state["lane"]["status"] == "awaiting_input"
    assert not any(
        e.event_type == "model.response"
        for e in sessions.read_range(state["lane"]["session_id"])
    )


def test_revoked_parent_capability_stops_previously_queued_child(env):
    service, _, _, model, context, root = env
    cap = service.open_model_parent(context)
    lane = service.spawn(
        command_id="spawn",
        parent_session_id=cap["parent_session_id"],
        task="x",
        workspace_root=str(root / "child"),
        context=context,
    )["lane"]["id"]
    service.revoke_model_parent(cap["parent_session_id"], cap["parent_token"], context)
    with pytest.raises(PermissionError):
        service.run_pending(lane, context)
    assert not model.requests


def test_model_dispatch_requires_verified_parent_binding(env):
    from sonder_runtime.interfaces.agent_lanes import dispatch_agent_lane_tool

    service, _, _, _, context, _ = env
    with pytest.raises(PermissionError):
        dispatch_agent_lane_tool(service, "list", {}, context, "parent")
    assert (
        dispatch_agent_lane_tool(
            service, "list", {}, context, "parent", bound_parent_session_id="parent"
        )["lanes"]
        == []
    )
