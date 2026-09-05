"""Prepared metadata drives the real managed workbench without discovery."""

from dataclasses import replace
import json
import pytest
import server
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.bootstrap.prepared_workbench import PreparedWorkbenchAdapter
from tests.test_tier_escalation import _install_agent_fakes
from tests.test_managed_standalone_controller import Session


@pytest.fixture
def prepared_host(monkeypatch, tmp_path):
    models = _install_agent_fakes(
        monkeypatch,
        {
            "m-code": "not a usable decision",
            "m-general": '{"final":"inspected repository"}',
        },
    )
    policy = {
        "revision": 1,
        "allowed_tools": ["read_file"],
        "allow_web": True,
        "allow_location": False,
    }
    context = local_owner_context(
        correlation_id="prepared", workspace_roots=(tmp_path,), timeout_seconds=120
    )
    adapter = PreparedWorkbenchAdapter(server, policy_snapshot=lambda: policy)
    return adapter, context, models, policy


def test_prepare_does_not_discover_or_generate_and_freezes_concrete_plan(
    prepared_host, monkeypatch
):
    adapter, context, models, policy = prepared_host

    def forbidden(*a, **k):
        raise AssertionError("provider/discovery invoked during preparation")

    monkeypatch.setattr(server, "_serve_target", forbidden)
    monkeypatch.setattr(server, "_make_generate", forbidden)
    value = adapter.prepare_workbench(
        {
            "prompt": "inspect repository",
            "tier": "auto",
            "max_steps": 64,
            "allow_web": False,
        },
        context,
    )
    assert value.spec.tier == "auto" and value.spec.resolved_model == "m-code"
    assert value.spec.max_steps == 20
    assert value.model_ladder[:2] == ("m-code", "m-general")
    assert models == []


def test_actual_workbench_executes_frozen_ladder_and_original_terminal_evidence(
    prepared_host,
):
    adapter, context, models, policy = prepared_host
    prepared = adapter.prepare_workbench(
        {"prompt": "inspect repository", "tier": "auto", "allow_web": False}, context
    )
    session = Session(context)
    controllers = []

    def factory(controller, application):
        controllers.append(controller)
        return session

    output = adapter.execute_prepared_workbench(
        prepared, admitted_context=context, managed_factory=factory
    )
    assert models == ["m-code", "m-general"]
    assert len(controllers) == 1 and session.calls.count("close") == 1
    assert "inspected repository" in output
    draft = controllers[0]._host_terminal
    records = json.loads(draft.ledger_bytes)["records"]
    assert any(row["tool"] == "host_rung" and not row["success"] for row in records)
    assert draft.output.endswith("inspected repository")


@pytest.mark.parametrize(
    "change",
    ["model", "policy", "endpoint", "root", "cancel", "web", "location", "cloud"],
)
def test_drift_refuses_before_factory_or_provider(
    prepared_host, monkeypatch, tmp_path, change
):
    adapter, context, models, policy = prepared_host
    prepared = adapter.prepare_workbench(
        {"prompt": "inspect repository", "allow_web": False}, context
    )
    if change == "model":
        monkeypatch.setitem(server.TIERS, "code", "changed-model")
    elif change == "policy":
        policy["revision"] = 2
    elif change == "endpoint":
        monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11999")
    elif change == "root":
        other = tmp_path / "other"
        other.mkdir()
        context = replace(context, workspace_roots=(other,))
    elif change == "cancel":
        from tests.test_app_managed_authority import Cancel

        token = Cancel()
        token.event.set()
        context = replace(context, cancellation=token)
    elif change == "web":
        policy["allow_web"] = False
    elif change == "location":
        policy["allow_location"] = True
    else:
        context = replace(context, cloud_allowed=True)
    factories = []
    with pytest.raises(PermissionError):
        adapter.execute_prepared_workbench(
            prepared,
            admitted_context=context,
            managed_factory=lambda *a: factories.append(a),
        )
    assert not factories and not models


def test_policy_change_during_actual_provider_call_stops_next_model(
    prepared_host, monkeypatch
):
    adapter, context, models, policy = prepared_host
    prepared = adapter.prepare_workbench(
        {"prompt": "inspect repository", "allow_web": False}, context
    )
    calls = []

    def generate(model, *a, **k):
        def call(*a, **k):
            calls.append(model)
            policy["revision"] = 2
            return "not a usable decision"

        return call

    monkeypatch.setattr(server, "_make_generate", generate)
    session = Session(context)
    with pytest.raises(PermissionError):
        adapter.execute_prepared_workbench(
            prepared, admitted_context=context, managed_factory=lambda *a: session
        )
    assert calls == ["m-code"]
    assert not adapter._active


def test_factory_failure_never_falls_back_to_local_owner(prepared_host, monkeypatch):
    from sonder_runtime.interfaces import standalone_agent_lanes as lanes

    adapter, context, models, policy = prepared_host
    prepared = adapter.prepare_workbench(
        {"prompt": "inspect repository", "tier": "code", "allow_web": False}, context
    )

    def forbidden(*a, **k):
        raise AssertionError("legacy parent minted")

    monkeypatch.setattr(lanes, "local_owner_context", forbidden)

    def factory(*a):
        raise PermissionError("private factory refused")

    with pytest.raises(PermissionError):
        adapter.execute_prepared_workbench(
            prepared, admitted_context=context, managed_factory=factory
        )
    assert not models and not adapter._active


def test_captured_model_callback_cannot_outlive_exact_invocation(
    prepared_host, monkeypatch
):
    adapter, context, models, policy = prepared_host
    prepared = adapter.prepare_workbench(
        {"prompt": "inspect repository", "tier": "code", "allow_web": False}, context
    )
    captured = []
    original = server._guard_managed_agent_call

    def guard(callback, **kwargs):
        result = original(callback, **kwargs)
        captured.append(result)
        return result

    monkeypatch.setattr(server, "_guard_managed_agent_call", guard)
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *a, **k: lambda *a, **k: '{"final":"inspected repository"}',
    )
    session = Session(context)
    adapter.execute_prepared_workbench(
        prepared, admitted_context=context, managed_factory=lambda *a: session
    )
    assert captured
    with pytest.raises(PermissionError):
        captured[0]("late request")
    assert not adapter._active


@pytest.mark.parametrize("tier", ["sonder", "local", "unconfigured-model"])
def test_discovery_dependent_targets_are_refused(prepared_host, monkeypatch, tier):
    adapter, context, models, policy = prepared_host
    monkeypatch.setattr(
        server,
        "_serve_target",
        lambda *a: (_ for _ in ()).throw(AssertionError("discovery")),
    )
    with pytest.raises(PermissionError):
        adapter.prepare_workbench(
            {"prompt": "inspect repository", "tier": tier}, context
        )
    assert not models


def test_private_tool_ceiling_canonicalizes_and_intersects(prepared_host, monkeypatch):
    from sonder_runtime.bootstrap.prepared_workbench import prepared_tool_allowlist

    adapter, context, models, policy = prepared_host
    policy["allowed_tools"] = ["agent_status"]
    prepared = adapter.prepare_workbench(
        {"prompt": "inspect repository", "tier": "code", "allow_web": False}, context
    )
    checked = []

    def generate(*a, **k):
        def call(*a, **k):
            assert prepared_tool_allowlist(
                {"master_status", "file_write"}
            ) == frozenset({"master_status"})
            assert prepared_tool_allowlist({"agent_status"}) == frozenset(
                {"master_status"}
            )
            with pytest.raises(PermissionError):
                server._agent_dispatch(
                    "file_write", {"path": "never.txt", "content": "denied"}
                )
            checked.append(True)
            return '{"final":"inspected repository"}'

        return call

    monkeypatch.setattr(server, "_make_generate", generate)
    adapter.execute_prepared_workbench(
        prepared, admitted_context=context, managed_factory=lambda *a: Session(context)
    )
    assert checked


def test_prepared_actual_edit_retains_failed_rung_evidence(
    prepared_host, monkeypatch, tmp_path
):
    from sonder_runtime.adapters.filesystem import file_ops
    from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger

    adapter, context, models, policy = prepared_host
    policy["allowed_tools"] = ["file_read", "file_write"]
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_agent_permission_gate_error", lambda *a, **k: None)
    (tmp_path / "source.txt").write_text("source")
    calls = []

    def generate(model, *a, **k):
        calls.append(model)
        replies = iter(
            [
                json.dumps(
                    {
                        "tool": "file_read",
                        "args": {"path": str(tmp_path / "source.txt")},
                    }
                ),
                json.dumps(
                    {
                        "tool": "file_write",
                        "args": {
                            "path": str(tmp_path / "edited.txt"),
                            "content": "real edit",
                        },
                    }
                ),
            ]
            if model == "m-code"
            else ['{"final":"inspected repository"}']
        )
        return lambda *a, **k: next(
            replies,
            "invalid JSON" if model == "m-code" else '{"final":"inspected repository"}',
        )

    monkeypatch.setattr(server, "_make_generate", generate)
    prepared = adapter.prepare_workbench(
        {"prompt": "inspect repository", "max_steps": 4, "allow_web": False}, context
    )
    controllers = []
    session = Session(context)

    def factory(controller, application):
        controllers.append(controller)
        return session

    output = adapter.execute_prepared_workbench(
        prepared, admitted_context=context, managed_factory=factory
    )
    assert calls == ["m-code", "m-general"]
    assert (tmp_path / "edited.txt").read_text() == "real edit"
    evidence = HostObservationLedger.restore(
        controllers[0]._host_terminal.ledger_bytes
    ).resolve()
    assert evidence.dirty and not evidence.parent_effects_valid
    assert "UNVERIFIED" in output or "VALIDATION_FAILED" in output
    assert session.calls.count("close") == 1
