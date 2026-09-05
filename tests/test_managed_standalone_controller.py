from dataclasses import replace
from types import SimpleNamespace
import pytest
from sonder_runtime.interfaces import standalone_agent_lanes as module
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.delegated_verification import VerificationVerdict


class Session:
    def __init__(self, context):
        self.context = context
        self.calls = []
        self.revoked = False

    def require_current(self):
        if self.revoked:
            raise PermissionError("revoked")
        self.calls.append("guard")

    def dispatch(self, prepared):
        self.calls.append(prepared)
        return {"managed": True}

    def report_metadata(self):
        self.calls.append("metadata")
        return {"managed": True}

    def request_cancel(self):
        self.calls.append("cancel")

    def close(self):
        self.calls.append("close")

    def verify_delegated(self, draft, *, verifier_factory):
        self.calls.append(draft)
        return VerificationVerdict(False, "APPROVAL_PENDING")


@pytest.fixture
def setup(tmp_path, monkeypatch):
    context = local_owner_context(
        correlation_id="host", workspace_roots=(tmp_path,), timeout_seconds=60
    )
    session = Session(context)

    def forbidden(*args, **kwargs):
        raise AssertionError("legacy authority minted")

    monkeypatch.setattr(module, "local_owner_context", forbidden)
    monkeypatch.setattr(module, "lane_service", forbidden)
    return session, SimpleNamespace()


def test_managed_dispatch_uses_private_snapshot_no_legacy_parent(setup):
    session, app = setup
    with module.managed_controller_factory_scope(
        lambda controller, application: session
    ):
        controller = module.StandaloneLaneController(lambda: app)
    command = controller.prepare_command({"action": "list", "payload": {}})
    detached = command.approval_arguments()
    detached["action"] = "cancel"
    assert controller.execute_prepared(command) == {"managed": True}
    assert command.approval_arguments()["action"] == "list"
    assert controller._context is session.context
    assert controller._parent is None
    with pytest.raises(PermissionError):
        controller.execute_prepared(replace(command, owner=object()))


def test_failed_factory_never_retries_or_falls_back(setup):
    session, app = setup
    calls = []

    def factory(controller, application):
        calls.append("factory")
        raise PermissionError("unavailable")

    with module.managed_controller_factory_scope(factory):
        controller = module.StandaloneLaneController(lambda: app)
    for _ in range(2):
        with pytest.raises(PermissionError):
            controller.prepare_command({"action": "list", "payload": {}})
    assert calls == ["factory"]
    assert controller._parent is None


def test_managed_cancel_close_and_metadata_are_separate(setup):
    session, app = setup
    with module.managed_controller_factory_scope(
        lambda controller, application: session
    ):
        controller = module.StandaloneLaneController(lambda: app)
    controller.require_current()
    assert controller.report_metadata() == {"managed": True}
    controller.close()
    controller.close()
    assert session.calls.count("close") == 1
    assert "cancel" not in session.calls
    with module.managed_controller_factory_scope(
        lambda controller, application: session
    ):
        other = module.StandaloneLaneController(lambda: app)
    other.require_current()
    other.request_cancel()
    other.request_cancel()
    assert session.calls.count("cancel") == 1


def test_host_terminal_guard_and_managed_verification_ignore_legacy_approval(setup):
    session, app = setup
    with module.managed_controller_factory_scope(
        lambda controller, application: session
    ):
        controller = module.StandaloneLaneController(lambda: app)
    controller.begin_host_turn(SimpleNamespace(seal=lambda: b"{}"))
    assert controller.freeze_host_terminal(
        "original", terminal_class="failed", blockers=("parent-failed",)
    )
    controller.delegated_work = True

    def forbidden(*args):
        raise AssertionError("legacy approval")

    verdict = controller.verify_delegated(forbidden, verifier_factory=forbidden)
    assert verdict.code == "APPROVAL_PENDING"
    assert any(
        isinstance(item, module.HostTerminalDraft) and item.output == "original"
        for item in session.calls
    )
    session.revoked = True
    with pytest.raises(PermissionError):
        controller.host_terminal_draft()


def test_nested_scopes_cannot_capture_managed_factory(setup):
    session, app = setup
    with module.managed_controller_factory_scope(
        lambda controller, application: session
    ):
        with module.controller_scope(lambda: app) as root:
            with module.controller_scope(lambda: app) as child:
                assert child is None
                nested = module.StandaloneLaneController(lambda: app)
                assert nested._managed_factory is None
            with module.model_loop_scope():
                nested = module.StandaloneLaneController(lambda: app)
                assert nested._managed_factory is None
        assert root._managed_factory is not None


def test_revoked_managed_session_blocks_prepared_command_and_evidence(setup):
    session, app = setup
    with module.managed_controller_factory_scope(
        lambda controller, application: session
    ):
        controller = module.StandaloneLaneController(lambda: app)
    command = controller.prepare_command({"action": "list", "payload": {}})
    session.revoked = True
    for action in (
        lambda: controller.execute_prepared(command),
        lambda: controller.prepare_command({"action": "list", "payload": {}}),
        lambda: controller.begin_host_turn(object()),
        lambda: controller.observe_host_tool(tool="write"),
        lambda: controller.freeze_host_terminal(
            "x", terminal_class="failed", blockers=()
        ),
    ):
        with pytest.raises(PermissionError):
            action()
    assert controller.report_metadata()["child_state"] == "unavailable"
    assert not any(
        isinstance(item, module.PreparedLaneCommand) for item in session.calls
    )


def test_managed_factory_and_controller_scope_restore_after_close_failure(setup):
    session, app = setup

    def close():
        raise RuntimeError("close failed")

    session.close = close
    with pytest.raises(RuntimeError):
        with module.managed_controller_factory_scope(
            lambda controller, application: session
        ):
            with module.controller_scope(lambda: app) as controller:
                controller.require_current()
    assert module.current() is None
    assert module._DEPTH.get() == 0
    assert module.StandaloneLaneController(lambda: app)._managed_factory is None
