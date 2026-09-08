"""Authorized interactive-lane retention lifecycle tests."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse


class Model:
    def generate(self, request, context):
        return ModelResponse("completed", "fake", request.tier, tokens_out=1)


@pytest.fixture
def env(tmp_path):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "fleet.db", sessions)
    service = AgentLaneService(store, sessions, Model(), auto_start=False)
    context = local_owner_context(correlation_id="retention", workspace_roots=(tmp_path,))
    root = tmp_path / "child"
    root.mkdir()
    return service, store, sessions, context, root


def spawn_completed(env, command="spawn"):
    service, _, _, context, root = env
    receipt = service.spawn(
        command_id=command,
        parent_session_id="parent",
        task="bounded task",
        workspace_root=str(root),
        context=context,
    )
    service.run_pending(receipt["lane"]["id"], context)
    return receipt["lane"]["id"]


def test_archive_requires_acknowledged_terminal_lane_and_releases_workspace(env):
    service, store, sessions, context, root = env
    lane_id = spawn_completed(env)
    report = service.reports("parent", context)["reports"][0]

    with pytest.raises(ValueError, match="acknowledge"):
        service.archive(lane_id, command_id="archive-before-ack", context=context)

    service.ack_report(report["id"], command_id="ack", context=context)
    result = service.archive(lane_id, command_id="archive", context=context)
    lane = result["lane"]
    assert lane["status"] == "archived"
    assert lane["archive_tombstone"]["message_count"] == 2
    assert len(lane["archive_tombstone"]["digest"]) == 64

    # Mailbox bodies and fleet agent metadata are retired, while the immutable
    # outbox/session event remains inspectable for audit.
    inspected = service.inspect(lane_id, context)
    assert inspected["messages"] == []
    assert any(event["event_type"] == "lane.archived" for event in inspected["events"])
    session_events = sessions.read_range(lane["session_id"])
    assert any(event.event_type == "lane.archived" for event in session_events)
    assert store.read_lane(lane_id)["status"] == "archived"
    with store._connection_scope() as conn:
        assert conn.execute(
            "SELECT 1 FROM fleet_agents WHERE id=?", (lane_id,)
        ).fetchone() is None

    # The archived workspace can be admitted again; it no longer consumes the
    # retained-lane or overlap reservation.
    replacement = service.spawn(
        command_id="replacement",
        parent_session_id="parent",
        task="reuse workspace",
        workspace_root=str(root),
        context=context,
    )
    assert replacement["lane"]["id"] != lane_id


def test_archive_is_idempotent_for_the_same_command_and_refuses_reopen(env):
    service, _, _, context, _ = env
    lane_id = spawn_completed(env)
    report = service.reports("parent", context)["reports"][0]
    service.ack_report(report["id"], command_id="ack", context=context)
    first = service.archive(lane_id, command_id="archive", context=context)
    assert service.archive(lane_id, command_id="archive", context=context) == first
    with pytest.raises(ValueError, match="already archived"):
        service.archive(lane_id, command_id="archive-again", context=context)
    with pytest.raises(ValueError, match="archived lane"):
        service.send_message(
            lane_id,
            command_id="message",
            content="reopen",
            author="user",
            context=context,
        )


def test_archive_refuses_active_or_uncertain_lane_and_foreign_principal(env):
    service, _, _, context, root = env
    queued = service.spawn(
        command_id="queued",
        parent_session_id="parent",
        task="not started",
        workspace_root=str(root),
        context=context,
    )["lane"]["id"]
    with pytest.raises(ValueError, match="terminal"):
        service.archive(queued, command_id="archive-queued", context=context)

    outsider = replace(context, principal_id="outsider")
    with pytest.raises(PermissionError):
        service.archive(queued, command_id="archive-outside", context=outsider)


def test_archive_is_available_through_http_and_repl_surfaces(env):
    from sonder_runtime.interfaces.http.facades.agent_lanes import (
        dispatch_agent_lane_route,
    )
    from sonder_runtime.interfaces.repl.facades.agent_lanes import (
        LaneConsoleFacade,
        parse,
    )

    service, _, _, context, root = env
    lane_id = spawn_completed(env)
    report = service.reports("parent", context)["reports"][0]
    service.ack_report(report["id"], command_id="ack-http", context=context)

    response = dispatch_agent_lane_route(
        service,
        "POST",
        "/v1/agent-lanes/" + lane_id + "/archive",
        {"command_id": "archive-http"},
        {},
        context,
    )
    assert response.status_code == 202
    assert response.body["lane"]["status"] == "archived"
    assert parse("archive " + lane_id) == ("archive", {"lane_id": lane_id})

    # The REPL uses the same scoped service and reports the lifecycle result;
    # an already archived lane remains readable but cannot be archived again.
    app = SimpleNamespace(
        agent_lanes=lambda: service,
        config=SimpleNamespace(
            state=SimpleNamespace(workspace_roots=(str(root.parent),)),
            ollama=SimpleNamespace(allow_remote=False),
        ),
    )
    ui = LaneConsoleFacade(lambda: app, lambda args: (True, ""))
    assert "already archived" in ui.run("archive " + lane_id)
