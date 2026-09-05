import json

import pytest
import server
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
from sonder_runtime.interfaces import standalone_agent_lanes as lanes
from tests.test_managed_standalone_controller import Session
from tests.test_tier_escalation import _install_agent_fakes
from tests.test_delegated_verification import lanes as lane_env


def test_real_workbench_uses_one_managed_owner_across_rungs(monkeypatch, tmp_path):
    models = _install_agent_fakes(
        monkeypatch,
        {
            "m-code": "not a usable tool decision",
            "m-general": '{"final":"general inspected the repository"}',
        },
    )
    session = Session(
        local_owner_context(
            correlation_id="escalate", workspace_roots=(tmp_path,), timeout_seconds=60
        )
    )
    controllers = []

    def factory(controller, application):
        assert not controllers, "single-use production factory was reused"
        controllers.append(controller)
        return session

    with lanes.managed_controller_factory_scope(factory):
        result = server.workbench_agent(
            prompt="inspect the repository", tier="auto", project=str(tmp_path)
        )
    assert models == ["m-code", "m-general"]
    assert "general inspected the repository" in result
    assert session.calls.count("close") == 1
    draft = controllers[0]._host_terminal
    assert draft is not None
    records = json.loads(draft.ledger_bytes)["records"]
    assert any(row["tool"] == "host_rung" and not row["success"] for row in records)
    assert draft.output.endswith("general inspected the repository")


@pytest.mark.parametrize("restriction", ["cloud", "read_only"])
def test_restricted_managed_host_stays_current_but_cannot_prepare_lane(
    tmp_path, restriction
):
    session = Session(
        local_owner_context(
            correlation_id="restricted", workspace_roots=(tmp_path,), timeout_seconds=60
        )
    )
    with lanes.managed_controller_factory_scope(lambda *args: session):
        with lanes.controller_scope(lambda: object()) as controller:
            controller.restrict(**{restriction: True})
            controller.require_current()
            assert (
                server._guard_managed_agent_call(lambda: "host call")() == "host call"
            )
            with pytest.raises(PermissionError):
                controller.prepare_command({"action": "list", "payload": {}})


def test_earlier_actual_write_survives_failed_rung_and_clean_final(
    monkeypatch, tmp_path
):
    from sonder_runtime.adapters.filesystem import file_ops

    _install_agent_fakes(monkeypatch, {})
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    monkeypatch.setattr(server, "_agent_permission_gate_error", lambda *a, **k: None)
    controllers = []
    session = Session(
        local_owner_context(
            correlation_id="write", workspace_roots=(tmp_path,), timeout_seconds=60
        )
    )
    calls = []
    (tmp_path / "source.txt").write_text("initial source")

    def factory(controller, application):
        assert not controllers
        controllers.append(controller)
        return session

    def make_generate(model, *args, **kwargs):
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
                            "path": str(tmp_path / "parent.txt"),
                            "content": "real edit",
                        },
                    }
                ),
            ]
            if model == "m-code"
            else ['{"final":"inspected final repository"}']
        )
        return lambda *a, **k: next(
            replies,
            (
                "not usable JSON"
                if model == "m-code"
                else '{"final":"inspected final repository"}'
            ),
        )

    monkeypatch.setattr(server, "_make_generate", make_generate)
    with lanes.managed_controller_factory_scope(factory):
        result = server.workbench_agent(
            prompt="inspect the repository",
            tier="auto",
            max_steps=4,
            project=str(tmp_path),
        )
    assert calls == ["m-code", "m-general"]
    assert (tmp_path / "parent.txt").exists(), result + repr(
        json.loads(controllers[0]._host_terminal.ledger_bytes)
    )
    assert (tmp_path / "parent.txt").read_text() == "real edit"
    draft = controllers[0]._host_terminal
    evidence = HostObservationLedger.restore(draft.ledger_bytes).resolve()
    assert evidence.dirty and not evidence.parent_effects_valid
    assert "VALIDATION_FAILED" in result or "UNVERIFIED" in result
    records = json.loads(draft.ledger_bytes)["records"]
    assert [r["tool"] for r in records].count("file_write") == 1
    assert session.calls.count("close") == 1


def test_private_rung_cannot_be_borrowed_by_nested_controller(tmp_path):
    session = Session(
        local_owner_context(
            correlation_id="nested", workspace_roots=(tmp_path,), timeout_seconds=60
        )
    )
    with lanes.managed_controller_factory_scope(lambda *args: session):
        with lanes.managed_escalation_scope(
            lambda: object(), project=str(tmp_path)
        ) as owner:
            with owner.rung():
                with lanes.controller_scope(
                    lambda: object(), project=str(tmp_path)
                ) as controller:
                    assert controller is owner.controller
                    controller.require_current()
                    with lanes.model_loop_scope():
                        with lanes.controller_scope(
                            lambda: pytest.fail("nested app")
                        ) as nested:
                            assert nested is None
                        direct = lanes.StandaloneLaneController(
                            lambda: pytest.fail("nested app")
                        )
                        with pytest.raises(PermissionError):
                            direct.require_current()
            assert not session.calls.count("close")
    assert session.calls.count("close") == 1


def test_no_approval_or_terminal_projection_until_outer_finalization(tmp_path):
    session = Session(
        local_owner_context(
            correlation_id="pending", workspace_roots=(tmp_path,), timeout_seconds=60
        )
    )
    with lanes.managed_controller_factory_scope(lambda *args: session):
        with lanes.managed_escalation_scope(
            lambda: object(), project=str(tmp_path)
        ) as owner:
            with owner.rung():
                with lanes.controller_scope(
                    lambda: object(), project=str(tmp_path)
                ) as controller:
                    controller.delegated_work = True
                    controller.begin_host_turn(
                        HostObservationLedger(project_scope=str(tmp_path))
                    )
                    controller.freeze_host_terminal(
                        "intermediate", terminal_class="ERROR", blockers=()
                    )
                    verdict = controller.verify_delegated(
                        lambda *a: pytest.fail("legacy approval"),
                        verifier_factory=lambda *a: pytest.fail("early verifier"),
                    )
                    assert not verdict.valid and controller._host_terminal is None

                    def finish():
                        assert controller.freeze_host_terminal(
                            "final", terminal_class="NORMAL", blockers=()
                        )
                        return controller.verify_delegated(
                            lambda *a: pytest.fail("legacy approval"),
                            verifier_factory=lambda *a: None,
                        )

                    owner.capture(finish)
            assert owner.finish().code == "APPROVAL_PENDING"
            assert controller._host_terminal.output == "final"
            assert (
                sum(isinstance(call, lanes.HostTerminalDraft) for call in session.calls)
                == 1
            )
            with pytest.raises(PermissionError):
                owner.finish()
            with pytest.raises(PermissionError):
                with owner.rung():
                    pass


def test_actual_registered_session_is_retained_then_detached_once(
    lane_env, monkeypatch
):
    from tests.test_managed_standalone_session import setup

    models = _install_agent_fakes(
        monkeypatch,
        {
            "m-code": "not a usable tool decision",
            "m-general": '{"final":"general inspected the repository"}',
        },
    )
    sessions = []
    identities = []

    def factory(controller, application):
        assert not sessions
        session, _, _, _ = setup(lane_env, controller)
        sessions.append(session)
        identities.append(session.report_metadata()["continuation_id"])
        return session

    with lanes.managed_controller_factory_scope(factory):
        result = server.workbench_agent(
            prompt="inspect the repository", tier="auto", project=str(lane_env[3])
        )
    assert models == ["m-code", "m-general"]
    assert "general inspected the repository" in result
    assert len(set(identities)) == 1
    assert sessions[0]._controller._parent is None
    with pytest.raises(PermissionError):
        sessions[0].require_current()


@pytest.mark.parametrize("cloud", [False, True])
def test_actual_restricted_loop_preserves_parent_model_admission(
    monkeypatch, tmp_path, cloud
):
    calls = []
    monkeypatch.setattr(
        server, "_serve_target", lambda *args: ("scripted", cloud, False, "test")
    )
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *a, **k: lambda *a, **k: (
            calls.append("model") or '{"final":"read-only result"}'
        ),
    )
    session = Session(
        local_owner_context(
            correlation_id="restricted-loop",
            workspace_roots=(tmp_path,),
            timeout_seconds=60,
        )
    )
    with lanes.managed_controller_factory_scope(lambda *args: session):
        with lanes.controller_scope(lambda: object()) as controller:
            result = server._agent_impl(
                "read the supplied text",
                read_only=not cloud,
                max_steps=1,
                auto_checklist=False,
            )
            assert "read-only result" in result
            assert not controller.available
            with pytest.raises(PermissionError):
                controller.prepare_command({"action": "spawn", "payload": {}})
    assert calls == ["model"]
