import json

import pytest

import runtime_policy
import server
import sonder_serve


@pytest.fixture
def isolated_runtime_policy(monkeypatch, tmp_path):
    original_tiers = dict(server.TIERS)
    original_policy = dict(server._RUNTIME_POLICY)
    path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(path))
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "sonder-home"))
    yield path
    server.TIERS.clear()
    server.TIERS.update(original_tiers)
    server._RUNTIME_POLICY = original_policy


def test_server_refresh_applies_external_policy_edit(isolated_runtime_policy):
    base_fallback = server.LOCAL_CODE_MODEL
    policy = runtime_policy.load(create=True)
    policy["local_models"]["code"] = "sonder-personal:latest"
    policy["routing"]["workbench"] = "general"
    isolated_runtime_policy.write_text(
        json.dumps({key: policy[key] for key in (
            "version", "revision", "local_models", "routing", "updated_ts", "source"
        )}),
        encoding="utf-8",
    )

    refreshed = server._refresh_runtime_policy(create=False)

    assert refreshed["local_models"]["code"] == "sonder-personal:latest"
    assert server.TIERS["code"] == "sonder-personal:latest"
    assert server.LOCAL_CODE_MODEL == base_fallback
    assert runtime_policy.route_tier("workbench", refreshed) == "general"


def test_guarded_update_requires_installed_local_model(
    isolated_runtime_policy, monkeypatch,
):
    monkeypatch.setattr(
        server,
        "_get",
        lambda _path: {"models": [
            {"name": "qwen2.5:3b"},
            {"name": "sonder:latest"},
            {"name": "qwen2.5:7b-instruct"},
        ]},
    )

    accepted = server.runtime_policy_update(
        local_models_json='{"general":"qwen2.5:7b-instruct"}',
        routing_json='{"review":"general"}',
    )
    missing = server.runtime_policy_update(
        local_models_json='{"code":"missing-local:latest"}',
    )
    cloud = server.runtime_policy_update(
        local_models_json='{"code":"qwen3-coder:480b-cloud"}',
    )

    assert "general: qwen2.5:7b-instruct" in accepted
    assert "review: general -> qwen2.5:7b-instruct" in accepted
    assert missing == "ERROR: local model(s) are not installed: missing-local:latest"
    assert cloud.startswith("ERROR:")


def test_runtime_slash_parses_models_and_routes(isolated_runtime_policy, monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "runtime_policy_update",
        lambda **kwargs: calls.append(kwargs) or "updated",
    )
    monkeypatch.setattr(server, "runtime_policy_status", lambda: "status")

    assert server.control_command("/runtime") == "status"
    assert server.control_command(
        "/runtime set code=sonder:latest workbench=general"
    ) == "updated"
    assert calls == [{
        "local_models_json": '{"code": "sonder:latest"}',
        "routing_json": '{"workbench": "general"}',
    }]
    assert server.control_command("/runtime reset") == "updated"
    assert calls[-1] == {"reset": True}


def test_runtime_http_status_is_safe_but_updates_require_developer():
    assert sonder_serve._dangerous_http_slash("/runtime") is False
    assert sonder_serve._dangerous_http_slash("/runtime status") is False
    assert sonder_serve._dangerous_http_slash("/runtime help") is False
    assert sonder_serve._dangerous_http_slash(
        "/runtime set workbench=general"
    ) is True
    assert sonder_serve._dangerous_http_slash("/runtime reset") is True


def test_runtime_policy_data_reports_missing_models(
    isolated_runtime_policy, monkeypatch,
):
    runtime_policy.load(create=True)
    runtime_policy.update(local_models={"general": "missing-general:latest"})
    monkeypatch.setattr(
        server,
        "_runtime_installed_models",
        lambda: {"qwen2.5:3b", "sonder:latest"},
    )

    data = server.runtime_policy_data()

    assert data["error"] == ""
    assert data["missing_models"] == ["missing-general:latest"]
    assert data["path"] == str(isolated_runtime_policy)


def test_installed_model_check_requires_the_requested_tag():
    installed = {"qwen2.5:3b", "sonder:latest", "bare-model"}

    assert server._runtime_model_is_installed("qwen2.5:3b", installed)
    assert not server._runtime_model_is_installed("qwen2.5:999b", installed)
    assert server._runtime_model_is_installed("sonder", installed)
    assert server._runtime_model_is_installed("bare-model:latest", installed)


def test_autopilot_reviewer_uses_shared_review_lane(
    isolated_runtime_policy, monkeypatch,
):
    runtime_policy.load(create=True)
    runtime_policy.update(routing={"review": "general"})
    selected = []

    def serve_target(tier, _augment):
        selected.append(tier)
        return "model-%s" % tier, False, False, tier

    monkeypatch.setattr(server, "_serve_target", serve_target)
    monkeypatch.setattr(server, "_build_system", lambda *_args, **_kwargs: "system")
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *_args, **_kwargs: lambda _prompt: '{"decision":"continue"}',
    )

    result = server._autopilot_json_model(
        {"tier": "code"},
        "reviewer",
        "review this",
        lambda payload: None,
    )

    assert result == {"decision": "continue"}
    assert selected == ["general"]


def test_lane_defaults_are_shared_but_explicit_tiers_win(
    isolated_runtime_policy,
):
    runtime_policy.load(create=True)
    runtime_policy.update(routing={
        "workbench": "general",
        "autopilot": "fast",
        "fleet": "general",
    })
    server._refresh_runtime_policy(create=False)

    assert server._runtime_lane_tier("workbench") == "general"
    assert server._runtime_lane_tier("autopilot", "auto") == "fast"
    assert server._runtime_lane_tier("fleet", "policy") == "general"
    assert server._runtime_lane_tier("fleet", "code") == "code"


def test_workbench_and_autopilot_use_shared_lane_defaults(
    isolated_runtime_policy, monkeypatch,
):
    runtime_policy.load(create=True)
    runtime_policy.update(routing={"workbench": "general", "autopilot": "fast"})
    agent_calls = []
    run_calls = []
    monkeypatch.setattr(
        server,
        "agent",
        lambda **kwargs: agent_calls.append(kwargs) or "workbench complete",
    )
    monkeypatch.setattr(
        server.autopilot_store,
        "create_run",
        lambda objective, **kwargs: (
            run_calls.append((objective, kwargs))
            or {"id": "auto-policy", "tier": kwargs["tier"]}
        ),
    )
    monkeypatch.setattr(server, "_launch_autopilot", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        server.autopilot_controller,
        "format_run",
        lambda run, **_kwargs: "tier=%s" % run["tier"],
    )

    assert server.workbench_agent("inspect this") == "workbench complete"
    output = server.autopilot_start("finish this")

    assert agent_calls[0]["tier"] == "general"
    assert run_calls[0][1]["tier"] == "fast"
    assert "tier=fast" in output


def test_master_uses_fleet_workers_and_review_lane_for_audit(
    isolated_runtime_policy, monkeypatch,
):
    runtime_policy.load(create=True)
    runtime_policy.update(routing={"fleet": "fast", "review": "general"})
    worker_tiers = []

    def worker(tier, **_kwargs):
        worker_tiers.append(tier)
        return lambda _task: "result"

    monkeypatch.setattr(server, "_orchestrator_worker", worker)
    monkeypatch.setattr(
        server.master_orchestrator,
        "requires_repository_tools",
        lambda _task: False,
    )
    monkeypatch.setattr(
        server.master_orchestrator,
        "run_delegated",
        lambda *_args, **_kwargs: {
            "master_id": "master-policy",
            "agents": ["agent-policy"],
            "worker_slots": 1,
            "output": "audited",
        },
    )

    output = server.master_orchestrate("summarize risks", mode="delegate", agents=1)

    assert "audited" in output
    assert worker_tiers == ["fast", "general"]
