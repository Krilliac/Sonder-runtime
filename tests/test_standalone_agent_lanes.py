import json
from types import SimpleNamespace

import pytest
import server
from tests.test_interactive_agent_lanes import env


def test_standalone_catalog_advertises_lane_controller():
    assert "- agent_lane:" in server.AGENT_TOOL_HELP


def app_for(env):
    service, _, _, _, _, root = env
    return SimpleNamespace(
        agent_lanes=lambda: service,
        config=SimpleNamespace(
            state=SimpleNamespace(workspace_roots=(str(root),)),
            ollama=SimpleNamespace(allow_remote=False),
        ),
    )


from sonder_runtime.interfaces import standalone_agent_lanes as lanes


def call(controller, action, **payload):
    return controller.execute({"action": action, "payload": payload})


def spawn(controller, root):
    return call(
        controller,
        "spawn",
        command_id="spawn",
        task="implement parser",
        workspace_root=str(root / "child"),
    )["lane"]


def test_catalog_and_dispatch_require_trusted_scope_even_unsafe(env):
    assert "- agent_lane:" not in server._agent_tool_help(unsafe=True)
    assert "HOST POLICY" in server._agent_dispatch(
        "agent_lane", {"action": "list", "payload": {}}
    )
    with lanes.controller_scope(lambda: app_for(env)):
        assert "- agent_lane:" in server._agent_tool_help(project_bound=True)
        assert "- agent_lane:" not in server._agent_tool_help(read_only=True)
        assert "- agent_lane:" not in server._agent_tool_help(cloud=True, unsafe=True)


def test_nested_scopes_never_mint_authority(env):
    with lanes.controller_scope(lambda: app_for(env)) as outer:
        assert lanes.current() is outer
        with lanes.controller_scope(lambda: app_for(env)) as child:
            assert child is None
            with lanes.controller_scope(lambda: app_for(env)) as grandchild:
                assert grandchild is None and lanes.current() is None
        assert lanes.current() is outer
    assert lanes.current() is None


@pytest.mark.parametrize(
    "key",
    [
        "parent_session_id",
        "parent_token",
        "author",
        "context",
        "workspace_roots",
        "cloud_allowed",
        "grant_revision",
    ],
)
def test_model_cannot_supply_authority(env, key):
    with lanes.controller_scope(lambda: app_for(env)) as controller:
        with pytest.raises(PermissionError):
            call(controller, "list", **{key: "claimed"})
        with pytest.raises(PermissionError):
            controller.prepare({"action": "list", "payload": {}, key: "claimed"})
        assert controller._parent is None


def test_workspace_intersection_and_service_budget_gates(env):
    root = env[-1]
    with lanes.controller_scope(
        lambda: app_for(env), project=str(root / "child")
    ) as controller:
        safe = controller.prepare(
            {"action": "spawn", "payload": {"workspace_root": "."}}
        )
        assert safe["payload"]["workspace_root"] == str((root / "child").resolve())
        with pytest.raises(PermissionError):
            call(
                controller,
                "spawn",
                command_id="outside",
                task="x",
                workspace_root=str(root),
            )
        with pytest.raises(ValueError):
            call(
                controller,
                "spawn",
                command_id="budget",
                task="x",
                workspace_root=".",
                max_steps=100,
            )


def test_private_parent_isolation_normal_exit_and_durable_reports(env):
    service, _, _, model, context, root = env
    with lanes.controller_scope(lambda: app_for(env)) as controller:
        first = spawn(controller, root)
        assert spawn(controller, root)["id"] == first["id"]
        token = controller._parent["parent_token"]
        assert token not in json.dumps(first)
        assert token not in json.dumps(
            controller.prepare({"action": "list", "payload": {}})
        )
        assert call(controller, "list")["lanes"][0]["id"] == first["id"]
        assert (
            call(controller, "inspect", lane_id=first["id"])["lane"]["id"]
            == first["id"]
        )
    assert controller._parent is None
    assert service.inspect(first["id"], context)["lane"]["status"] == "queued"
    service.run_pending(first["id"], context)
    assert len(model.requests) == 1
    assert token not in model.requests[0][0].prompt
    assert service.reports(first["parent_session_id"], context)["reports"]
    with lanes.controller_scope(lambda: app_for(env)) as unrelated:
        assert call(unrelated, "list")["lanes"] == []
        with pytest.raises(PermissionError):
            call(unrelated, "inspect", lane_id=first["id"])


def test_all_controller_actions_use_shared_service(env):
    service, _, _, model, context, root = env
    with lanes.controller_scope(lambda: app_for(env)) as controller:
        lane = spawn(controller, root)["id"]
        call(
            controller,
            "message",
            lane_id=lane,
            command_id="message",
            content="Preserve Unicode",
        )
        call(controller, "interrupt", lane_id=lane, command_id="interrupt")
        service.run_pending(lane, context)
        call(
            controller, "resume", lane_id=lane, command_id="resume", content="Continue"
        )
        service.run_pending(lane, context)
        assert (
            call(controller, "wait", lane_id=lane, timeout_seconds=0)["lane"]["status"]
            == "completed"
        )
        reports = call(controller, "report")["reports"]
        assert reports
        call(controller, "ack", report_id=reports[0]["id"], command_id="ack")
        assert call(controller, "reports")["reports"][0]["acknowledged"]
        call(controller, "cancel", lane_id=lane, command_id="cancel")


def test_explicit_parent_cancellation_cancels_without_model_effects(env):
    service, _, _, model, context, root = env
    with lanes.controller_scope(lambda: app_for(env)) as controller:
        lane = spawn(controller, root)["id"]
        controller.request_cancel()
        assert not controller.available
        with pytest.raises(PermissionError):
            call(controller, "list")
    service.run_pending(lane, context)
    assert model.requests == []
    assert service.inspect(lane, context)["lane"]["status"] == "cancelled"


def test_dispatch_gate_has_run_identity_but_no_bearer(env, monkeypatch):
    captured = []

    def deny(name, args):
        captured.append((name, args))
        return "ERROR: denied by configured permission mode"

    monkeypatch.setattr(server, "_agent_permission_gate_error", deny)
    with lanes.controller_scope(lambda: app_for(env)) as controller:
        result = server._agent_dispatch("agent_lane", {"action": "list", "payload": {}})
        assert "denied" in result and controller._parent is None
        assert captured[0][1]["standalone_run_id"] == controller.run_id
        assert "token" not in json.dumps(captured)


def test_root_agent_entrypoint_binds_and_discards_context(env, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "_application", lambda: app_for(env))

    def loop(*args, **kwargs):
        seen.append(lanes.current())
        assert lanes.current() is not None
        return json.dumps(call(lanes.current(), "list"))

    monkeypatch.setattr(server, "_agent_impl", loop)
    result = server.agent("inspect child lanes", checklist=False)
    assert seen and not seen[0].available and lanes.current() is None
    assert "lanes" in result


def test_nested_model_loop_cannot_inherit_controller(env, monkeypatch):
    seen = []

    def turn(prompt, **kwargs):
        seen.append(lanes.current())
        if prompt == "root":
            server._agent_impl("nested")
        return "done"

    monkeypatch.setattr(server, "_agent_turn", turn)
    with lanes.controller_scope(lambda: app_for(env)) as controller:
        server._agent_impl("root")
        assert seen == [controller, None]
        assert lanes.current() is controller


def test_unsafe_mode_cannot_relax_lane_read_only(env, monkeypatch):
    monkeypatch.setattr(server.unsafe_lab, "active", lambda: True)
    with lanes.controller_scope(lambda: app_for(env)) as controller:
        result = server._agent_dispatch(
            "agent_lane", {"action": "list", "payload": {}}, read_only=True
        )
        assert "HOST POLICY" in result and controller._parent is None


def test_actual_model_loop_dispatches_lane_and_sees_no_bearer(env, monkeypatch):
    prompts = []
    responses = iter(
        [
            json.dumps(
                {
                    "tool": "agent_lane",
                    "args": {
                        "action": "spawn",
                        "payload": {
                            "command_id": "model-spawn",
                            "task": "implement parser",
                            "workspace_root": str(env[-1] / "child"),
                        },
                    },
                }
            ),
            json.dumps(
                {"tool": "agent_lane", "args": {"action": "list", "payload": {}}}
            ),
            json.dumps({"final": "Child delegated."}),
        ]
    )

    def generate(prompt, history=None):
        prompts.append(prompt)
        return next(responses, json.dumps({"final": "Child delegated."}))

    monkeypatch.setattr(server, "_application", lambda: app_for(env))
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(server, "_agent_permission_gate_error", lambda *a, **k: None)
    with lanes.controller_scope(lambda: app_for(env)) as controller:
        result = server._agent_impl(
            "Delegate implementation to an independent lane",
            max_steps=4,
            auto_checklist=False,
        )
        children = call(controller, "list")["lanes"]
        assert len(children) == 1
        assert result.startswith("UNVERIFIED:")
        assert "DELEGATED WORK METADATA" in result
        assert children[0]["id"] in result
        assert '"revision"' in result
        token = controller._parent["parent_token"]
        assert token not in result and token not in "\n".join(prompts)
        assert any("agent_lane" in prompt for prompt in prompts)


@pytest.mark.parametrize(
    "action,expected",
    [
        ("spawn", True),
        ("message", True),
        ("resume", True),
        ("list", False),
        ("wait", False),
    ],
)
def test_lane_effects_invalidate_workspace_verification(action, expected):
    assert (
        server._agent_tool_mutates("agent_lane", {"action": action, "payload": {}})
        is expected
    )
