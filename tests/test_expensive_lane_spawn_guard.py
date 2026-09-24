"""Expensive lane admission counts the durable parent history, including archives."""

import json
from dataclasses import replace

import pytest

from sonder_runtime.application.agents.interactive_lanes import (
    _MAX_EXPENSIVE_LANES_PER_PARENT,
)

pytest_plugins = ("tests.test_delegated_verification",)


def test_expensive_spawn_cap_counts_archives_across_service_restart(lanes):
    service, store, model, root, context, parent = lanes
    first = root / "first"
    first.mkdir()
    created = service.spawn(
        command_id="first",
        parent_session_id=parent["parent_session_id"],
        task="reason carefully",
        workspace_root=str(first),
        tier="reasoning",
        context=context,
    )
    first_id = created["lane"]["id"]
    with store.transaction() as tx:
        row = tx.lane(first_id)
        assert row["expensive_tier"] is True
        # A historic archive still occupies this parent's spend allowance.
        row["status"] = "archived"
        tx.save(row)
        for i in range(_MAX_EXPENSIVE_LANES_PER_PARENT - 1):
            archived = dict(row, id=f"archived-{i}", session_id=f"session-{i}")
            tx.conn.execute(
                "INSERT INTO agent_lanes(id,principal,parent_session,data) VALUES (?,?,?,?)",
                (archived["id"], context.principal_id,
                 parent["parent_session_id"], json.dumps(archived)),
            )
    # Reconstructing the service has no effect on durable admission counts.
    from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
    from sonder_runtime.application.agents.interactive_lanes import AgentLaneService

    reopened_store = SQLiteAgentLaneStore(store.path, service.sessions)
    reopened = AgentLaneService(
        reopened_store, service.sessions, model, auto_start=False
    )
    second = root / "second"
    second.mkdir()
    with pytest.raises(ValueError, match="expensive lane spawn"):
        reopened.spawn(
            command_id="blocked",
            parent_session_id=parent["parent_session_id"],
            task="reason again",
            workspace_root=str(second),
            tier="reasoning",
            context=context,
        )
    with reopened_store.transaction() as tx:
        assert tx.lanes(context.principal_id, parent["parent_session_id"], limit=100)[-1][1]["id"] != "blocked"


def test_expensive_spawn_cap_tracks_resolved_hosted_code_tier(lanes):
    service, store, model, root, context, parent = lanes
    from sonder_runtime.application.ports.model_target import ResolvedModelRoute

    model.resolve_route = lambda request, _context: ResolvedModelRoute(
        "ollama", "provider:cloud", request.tier, request.tier, True, "", "", object()
    )
    context = replace(context, cloud_allowed=True)
    created = service.spawn(
        command_id="hosted-code", parent_session_id=parent["parent_session_id"],
        task="write code", workspace_root=str(root), tier="code", context=context,
    )
    with store.transaction() as tx:
        assert tx.lane(created["lane"]["id"])["expensive_tier"] is True


def test_replayed_command_uses_durable_receipt_if_route_changes(lanes):
    service, _store, model, root, context, parent = lanes
    from sonder_runtime.application.ports.model_target import ResolvedModelRoute

    calls = []

    def resolve(request, _context):
        calls.append(request.tier)
        if len(calls) != 1:
            raise ValueError("route changed after first admission")
        return ResolvedModelRoute(
            "ollama", "local:latest", request.tier, "code", False, "", "", object()
        )

    model.resolve_route = resolve
    request = {
        "command_id": "stable-command",
        "parent_session_id": parent["parent_session_id"],
        "task": "write code", "workspace_root": str(root),
        "tier": "code", "context": context,
    }
    receipt = service.spawn(**request)
    assert service.spawn(**request) == receipt
    assert calls == ["code"]


def test_local_lane_cannot_be_rebound_to_hosted_tier_after_spawn(lanes):
    service, store, model, root, context, parent = lanes
    from sonder_runtime.application.ports.model_target import ResolvedModelRoute

    target = ["local:latest"]
    context = replace(context, cloud_allowed=True)
    model.resolve_route = lambda request, _context: ResolvedModelRoute(
        "ollama", target[0], request.tier, "code",
        target[0].endswith(":cloud"), "", "", object(),
    )
    receipt = service.spawn(
        command_id="local-at-admission", parent_session_id=parent["parent_session_id"],
        task="write code", workspace_root=str(root), tier="code", context=context,
    )
    target[0] = "provider:cloud"
    lane_id = receipt["lane"]["id"]
    service.run_pending(lane_id, context)
    with store.transaction() as tx:
        assert tx.lane(lane_id)["status"] == "failed"
    assert model.calls == 0
