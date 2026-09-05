"""The standalone consumer binds typed certificates to its private parent."""
from dataclasses import replace
from types import SimpleNamespace
import pytest
from tests.test_delegated_verification import lanes as lane_env, _verifier
from sonder_runtime.interfaces.standalone_agent_lanes import StandaloneLaneController


@pytest.fixture
def consumer(lane_env):
    service, store, model, root, _, _ = lane_env
    app = SimpleNamespace(agent_lanes=lambda: service, config=SimpleNamespace(
        state=SimpleNamespace(workspace_roots=(str(root),)),
        ollama=SimpleNamespace(allow_remote=False)))
    controller = StandaloneLaneController(lambda: app)
    child = controller.execute({"action": "spawn", "payload": {
        "command_id": "spawn-consumer", "task": "bounded change", "workspace_root": str(root),
    }})["lane"]
    service.run_pending(child["id"], controller._context)
    verifier, gateway, proofs = _verifier((
        service, store, model, root, controller._context, controller._parent))
    return controller, verifier, gateway, root


def test_consumer_approves_once_and_freshly_validates_source(consumer):
    controller, verifier, gateway, root = consumer
    approvals = []
    def approve(prepared, context):
        approvals.append(prepared.approval_payload())
        assert context is controller._context
        return "host-approval"
    factory = lambda app, service: verifier
    first = controller.verify_delegated(approve, verifier_factory=factory)
    assert first.valid is True
    assert first.parent_session_id == controller._parent["parent_session_id"]
    assert controller.verify_delegated(approve, verifier_factory=factory) == first
    assert len(approvals) == 1 and gateway.calls == 1
    (root / "untracked.txt").write_text("changed after certification")
    assert controller.verify_delegated(approve, verifier_factory=factory).valid is False
    assert gateway.calls == 1


@pytest.mark.parametrize("field,value", [
    ("parent_session_id", "wrong-parent"), ("parent_grant_revision", 999),
    ("generation", 999), ("roots", ("unrelated",)), ("children", ()),
    ("certificate_id", "wrong-certificate"), ("valid", 1), ("code", "UNKNOWN"),
])
def test_consumer_refuses_misbound_typed_verdict(consumer, monkeypatch, field, value):
    controller, verifier, gateway, root = consumer
    factory = lambda app, service: verifier
    verdict = controller.verify_delegated(lambda *args: "approval", verifier_factory=factory)
    assert verdict.valid is True
    monkeypatch.setattr(verifier, "validate", lambda *args, **kwargs: replace(verdict, **{field: value}))
    assert controller.verify_delegated(lambda *args: "approval", verifier_factory=factory).valid is False


def test_consumer_never_relaunches_after_ambiguous_execution_response(consumer, monkeypatch):
    controller, verifier, gateway, root = consumer
    execute = verifier.execute_prepared
    def lost_response(*args, **kwargs):
        execute(*args, **kwargs)
        raise OSError("lost response")
    monkeypatch.setattr(verifier, "execute_prepared", lost_response)
    factory = lambda app, service: verifier
    assert not controller.verify_delegated(lambda *args: "approval", verifier_factory=factory).valid
    assert controller.verify_delegated(lambda *args: "approval", verifier_factory=factory).valid
    assert gateway.calls == 1
