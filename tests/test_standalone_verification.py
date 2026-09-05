"""The standalone consumer binds typed certificates to its private parent."""
from dataclasses import replace
from types import SimpleNamespace
import json
import pytest
from tests.test_delegated_verification import lanes as lane_env, _verifier
from tests.test_lane_coding_acceptance import coding, make_service, tool
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


@pytest.mark.parametrize("parent_edit,failed", [(False, False), (True, False), (False, True)])
def test_real_loop_projects_parent_and_child_validation_separately(consumer, monkeypatch, parent_edit, failed):
    import server
    from sonder_runtime.interfaces import standalone_agent_lanes
    from sonder_runtime.adapters.filesystem import file_ops
    controller, verifier, gateway, root = consumer
    responses = []
    if parent_edit:
        responses.append({"tool": "file_write", "args": {"path": str(root / "parent.txt"), "content": "parent change"}})
    responses.append({"final": "ERROR: incomplete task" if failed else "Work complete."})
    turns = iter(json.dumps(value) for value in responses)
    monkeypatch.setattr(server, "_make_generate", lambda *args, **kwargs: lambda *a, **k: next(turns, json.dumps(responses[-1])))
    def factory(*args):
        assert controller.host_terminal_draft().output.startswith("Work complete.")
        return verifier
    monkeypatch.setattr(server, "_standalone_verifier_factory", factory)
    monkeypatch.setattr(server, "_agent_permission_gate_error", lambda *args, **kwargs: None)
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)
    token = standalone_agent_lanes._CURRENT.set(controller)
    try:
        result = server._agent_impl("Complete the bounded work", max_steps=3, return_host_receipt=True)
    finally:
        standalone_agent_lanes._CURRENT.reset(token)
    assert result.validation_passed is (not parent_edit and not failed)
    assert result.mutation_observed is parent_edit
    if not failed:
        assert result.verification_code == "CERTIFIED"
        assert result.verification_certificate_id
        assert gateway.calls == 1
    else:
        assert result.output.startswith("ERROR:")
        assert gateway.calls == 0
    if parent_edit:
        assert (root / "parent.txt").read_text() == "parent change"
        assert "UNVERIFIED:" in result.output
    # Freeze actual host evidence before delegated approval/final decoration.
    from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
    draft = controller.host_terminal_draft()
    assert draft.output.startswith("ERROR:" if failed else "Work complete.")
    assert "Delegated workspace certificate" not in draft.output
    assert HostObservationLedger.restore(draft.ledger_bytes).resolve().dirty is parent_edit
    assert draft.terminal_class == ("ERROR" if failed else "NORMAL")


@pytest.mark.parametrize("text", ["  CANCELLED by host", "  EVIDENCE_REQUIRED missing evidence"])
def test_whitespace_failure_never_enters_delegated_gate(consumer, monkeypatch, text):
    import server
    from sonder_runtime.interfaces import standalone_agent_lanes
    controller, verifier, gateway, root = consumer
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: lambda *a, **k: json.dumps({"final": text}))
    def forbidden(*args, **kwargs):
        pytest.fail("failure entered delegated verifier")
    monkeypatch.setattr(server, "_standalone_verifier_factory", forbidden)
    token = standalone_agent_lanes._CURRENT.set(controller)
    try:
        result = server._agent_impl("Report current state", max_steps=1, return_host_receipt=True)
    finally:
        standalone_agent_lanes._CURRENT.reset(token)
    assert result.output.startswith(text.lstrip().split()[0])
    assert result.validation_passed is False
    assert gateway.calls == 0


def test_terminal_draft_freeze_refuses_replacement(consumer):
    from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
    controller, _, _, root = consumer
    controller.begin_host_turn(HostObservationLedger(project_scope=str(root)))
    assert controller.freeze_host_terminal("original", terminal_class="NORMAL", blockers=())
    assert controller.freeze_host_terminal("original", terminal_class="NORMAL", blockers=())
    assert not controller.freeze_host_terminal("replacement", terminal_class="NORMAL", blockers=())
    assert controller._host_terminal.output == "original"


def test_aborting_dispatched_failure_is_in_terminal_ledger(consumer, monkeypatch):
    import server
    from sonder_runtime.interfaces import standalone_agent_lanes
    controller, _, gateway, _ = consumer
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: lambda *a, **k: json.dumps({
        "tool": "web_search", "args": {"query": "bounded query"}}))
    monkeypatch.setattr(server, "_agent_permission_gate_error", lambda *a, **k: None)
    monkeypatch.setattr(server, "_agent_dispatch_observed", lambda *a, **k: "ERROR: source unavailable")
    token = standalone_agent_lanes._CURRENT.set(controller)
    try:
        result = server._agent_impl("Look up a fact", max_steps=1, allow_web=True,
            abort_on_tool_failure_names=("web_search",), return_host_receipt=True)
    finally:
        standalone_agent_lanes._CURRENT.reset(token)
    records = json.loads(controller.host_terminal_draft().ledger_bytes)["records"]
    assert any(r["tool"] == "web_search" and r["dispatched"] and not r["success"] for r in records)
    assert result.output.startswith("ERROR:")
    assert gateway.calls == 0


def test_standalone_composed_catalog_certifies_real_repair_and_diff(coding, monkeypatch):
    import subprocess
    import server
    from sonder_runtime.interfaces import standalone_agent_lanes
    repo, catalog_path, store, sessions, jobs, provider, facade, context = coding
    service, model = make_service(coding, [
        tool("run_tests", target="unit"),
        tool("edit_file", path="calc.py", old="return sum(values) + 1", new="return sum(values)"),
        tool("run_tests", target="unit"), "Repaired",
    ])
    independent = catalog_path.with_name("independent-tests.json")
    catalog = json.loads(catalog_path.read_text())
    catalog["targets"] = [target for target in catalog["targets"] if target["name"] == "unit"]
    independent.write_text(json.dumps(catalog))
    monkeypatch.setenv("SONDER_LANE_TEST_TARGETS_FILE", str(independent))
    app = SimpleNamespace(agent_lanes=lambda: service, process_job_provider=lambda: provider,
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(str(repo),)),
                               ollama=SimpleNamespace(allow_remote=False)))
    approvals = []
    def approve(name, args):
        approvals.append((name, args))
        return None
    monkeypatch.setattr(server, "_agent_permission_gate_error", approve)
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: lambda *a, **k: json.dumps({"final": "Repair ready for review."}))
    with standalone_agent_lanes.controller_scope(lambda: app) as controller:
        child = controller.execute({"action": "spawn", "payload": {
            "command_id": "real-repair", "task": "Repair total and test it",
            "workspace_root": str(repo), "max_steps": 8,
        }})["lane"]
        service.run_pending(child["id"], controller._context)
        assert service.inspect(child["id"], controller._context)["lane"]["status"] == "completed"
        result = server._agent_impl("Return the reviewed work", return_host_receipt=True)
        assert result.validation_passed is True, result.output
        assert result.verification_code == "CERTIFIED"
        assert result.receipt()["delegated_verification"]["certificate_id"]
        assert result.mutation_observed is False  # Parent did not perform the child edit.
        assert len(approvals) == 1 and approvals[0][0] == "workspace_run"
        assert approvals[0][1]["checks"][0]["argv"] == catalog["targets"][0]["argv"]
        assert controller._parent["parent_token"] not in result.output
    diff = subprocess.run(["git", "diff", "--", "calc.py"], cwd=repo, capture_output=True,
                          text=True, check=True, timeout=10).stdout
    assert "+    return sum(values)" in diff


@pytest.mark.parametrize("kind", ["inspect", "research", "report", "implement", "validate"])
def test_cancelled_delegated_receipt_never_passes_task(consumer, kind):
    from autopilot_controller import HostTaskResult, _task_passed
    controller = consumer[0]
    output = controller.report_outcome("CANCELLED: operator stopped this task")
    result = HostTaskResult(output, tools=("file_read",), mutation_observed=True,
                            validation_attempted=True, validation_passed=True)
    passed, reason = _task_passed(result, {"kind": kind})
    assert passed is False
    assert reason.startswith("CANCELLED:")
