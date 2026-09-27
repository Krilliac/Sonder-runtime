"""Permission modes for the build tools, and approvals bound to the planned command.

The gateway's evaluator plans ``build_job`` and ``build_fix`` before the
permission modes decide (``bootstrap.build_tools.build_permission_resolvers``),
so an approval binds to the host-resolved command, a plan the host refuses is
refused before anyone is asked, and a build that asks for the network needs a
second, separate ``build_network`` decision.
"""
from __future__ import annotations

import pytest

import permission_modes as pm
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.domain.common.errors import Forbidden
from tests.test_build_executor import (
    compose_facade,
    fake_services,
    gateway_call,
    output,
)

pytestmark = pytest.mark.unit


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    pm.reset_unattended_for_tests()
    yield store
    pm.forget_spent_approval()
    pm.reset_unattended_for_tests()


@pytest.fixture
def mode(monkeypatch):
    def set_mode(value):
        monkeypatch.setattr(pm, "current_mode", lambda: value)
    return set_mode


def test_risk_grades():
    for name in ("build_job", "build_fix"):
        assert pm.risk_of(name) == "execution", name
    for name in ("build_model", "build_job_result", "build_fix_result"):
        assert pm.risk_of(name) == "safe", name
    assert pm.risk_of("build_fix_restore") == "mutation"
    assert pm.risk_of("build_network") == "execution"


def test_the_execution_grade_is_what_the_gate_reads(monkeypatch):
    """Mutation proof: without the native execution set build_job is not execution."""
    monkeypatch.setattr(pm, "NATIVE_EXECUTION_TOOLS", frozenset())
    monkeypatch.setattr(pm, "NATIVE_MCP_WORK", {k: v for k, v in pm.NATIVE_MCP_WORK.items()
                                                if k != "build_job"})
    assert pm.risk_of("build_job") != "execution"


@pytest.mark.parametrize("tool, arguments", [
    ("build_job", {"target": "game"}), ("build_fix", {"target": "game"}),
])
def test_plan_mode_refuses_before_planning(tmp_path, ledger, mode, tool, arguments):
    mode(pm.PLAN)
    services = fake_services(tmp_path, tmp_path / "build")
    tools, audit, grants = compose_facade(tmp_path, services)
    with pytest.raises(Forbidden):
        gateway_call(tools, tool, arguments)
    assert services.jobs.planned == [] and services.fix.planned == []
    assert services.jobs.runs == [] and services.fix.started == []
    assert audit.read()[-1]["terminal"] == "policy_denied"
    assert len(grants) == 0
    # the model reader stays available in plan mode
    assert gateway_call(tools, "build_model", {}).success


@pytest.mark.parametrize("current", [pm.MANUAL, pm.ACCEPT_EDITS])
@pytest.mark.parametrize("source", ["mcp", "http", "worker"])
def test_unattended_manual_and_accept_edits_refuse_builds_with_a_call_id(
        tmp_path, ledger, mode, current, source):
    mode(current)
    services = fake_services(tmp_path, tmp_path / "build")
    tools, _, grants = compose_facade(tmp_path, services)
    for tool in ("build_job", "build_fix"):
        with pytest.raises(Forbidden) as caught:
            gateway_call(tools, tool, {"target": "game"}, source=source)
        assert caught.value.decision["call_id"], "the refusal names the call for an approval"
        assert "unattended" in caught.value.decision["source"]
    assert services.jobs.runs == [] and services.fix.started == [] and len(grants) == 0


def test_a_console_operator_is_asked_once_and_the_surface_decides(tmp_path, ledger, mode):
    """manual at an interactive console: the REPL prompts, then forwards gate=surface."""
    mode(pm.MANUAL)
    services = fake_services(tmp_path, tmp_path / "build")
    tools, audit, grants = compose_facade(tmp_path, services)
    receipt = gateway_call(tools, "build_fix", {"target": "game"}, source="repl", gate="surface")
    assert receipt.success, receipt.output
    assert receipt.policy_match.endswith("permission:surface")
    assert len(services.fix.planned) == 1, "start() runs exactly the plan that was approved"
    assert services.fix.started[0][2].startswith("build_fix_grant:"), \
        "the approval became the fix's grant"
    assert ledger.pending() == []


def test_auto_allows_builds_and_fixes(tmp_path, ledger, mode):
    mode(pm.AUTO)
    services = fake_services(tmp_path, tmp_path / "build")
    tools, _, grants = compose_facade(tmp_path, services)
    job = gateway_call(tools, "build_job", {"target": "game"})
    assert job.success and output(job)["status"] == "running"
    fix = gateway_call(tools, "build_fix", {"target": "game"})
    assert fix.success and output(fix)["grant"] == "bound"
    assert len(grants) == 1


def _approve(ledger, services, tool, arguments):
    from tests.test_build_executor import FixRequestDouble, JobRequestDouble

    if tool == "build_job":
        plan = services.jobs.plan(JobRequestDouble(**{"action": "build", **arguments}), None)
        services.jobs.planned.clear()
    else:
        plan = services.fix.plan(FixRequestDouble(**arguments), None)
        services.fix.planned.clear()
    approved = {**arguments, "resolved_command": plan.resolved_command()}
    return ledger.issue(tool, pm.call_digest(tool, approved), approver="test")


def test_a_job_approval_is_bound_to_the_command_digest_and_template(
        tmp_path, ledger, mode):
    mode(pm.MANUAL)
    services = fake_services(tmp_path, tmp_path / "build")
    tools, _, _ = compose_facade(tmp_path, services)
    arguments = {"target": "game", "config": "Debug"}
    grant = _approve(ledger, services, "build_job", arguments)
    assert pm.call_digest("build_job", arguments) != pm.call_digest(
        "build_job", {**arguments, "resolved_command": {}}), "the bare call never matches"
    for other in ({"target": "core", "config": "Debug"}, {"target": "game", "config": "Release"},
                  {"target": "game", "config": "Debug", "platform": "x64"}):
        with pytest.raises(Forbidden):
            gateway_call(tools, "build_job", other)
    assert gateway_call(tools, "build_job", arguments).success
    with pytest.raises(Forbidden):
        gateway_call(tools, "build_job", arguments)  # spent
    assert ledger.get(grant.nonce).spent
    assert [request.target for request, _, _ in services.jobs.runs] == ["game"]


def test_a_refused_plan_is_refused_before_anyone_is_asked(tmp_path, ledger, mode):
    for current in (pm.MANUAL, pm.AUTO):
        mode(current)
        services = fake_services(tmp_path, tmp_path / "build")
        tools, audit, _ = compose_facade(tmp_path, services)
        for tool, target, code in (("build_job", "nope", "UNKNOWN_TARGET"),
                                   ("build_job", "deploy", "UTILITY_TARGET_REFUSED"),
                                   ("build_fix", "nope", "UNKNOWN_TARGET")):
            with pytest.raises(Forbidden) as caught:
                gateway_call(tools, tool, {"target": target})
            assert caught.value.decision == {"tool": tool, "error_code": code, "stage": "plan"}
            assert caught.value.policy_match == "build:plan-refused"
        assert ledger.pending() == [], "nothing was put in front of the operator"
        assert services.jobs.runs == [] and services.fix.started == []


def test_allow_network_needs_its_own_decision(tmp_path, ledger, mode, monkeypatch):
    mode(pm.AUTO)
    monkeypatch.setattr(pm, "_rule_lookup", lambda tool: {"action": "deny", "pattern": tool}
                        if tool == "build_network" else None)
    services = fake_services(tmp_path, tmp_path / "build")
    tools, _, grants = compose_facade(tmp_path, services)
    assert gateway_call(tools, "build_job", {"target": "game"}).success
    for tool in ("build_job", "build_fix"):
        with pytest.raises(Forbidden) as caught:
            gateway_call(tools, tool, {"target": "game", "allow_network": True})
        assert caught.value.decision["tool"] == "build_network"
    assert len(services.jobs.runs) == 1 and services.fix.started == []
    assert len(grants) == 0, "a fix refused the network mints no grant"
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    assert gateway_call(tools, "build_job", {"target": "game", "allow_network": True}).success


def test_manual_network_decision_is_refused_unattended_even_after_a_surface_prompt(
        tmp_path, ledger, mode):
    mode(pm.MANUAL)
    services = fake_services(tmp_path, tmp_path / "build")
    tools, _, _ = compose_facade(tmp_path, services)
    assert gateway_call(tools, "build_job", {"target": "game"}, source="repl", gate="surface").success
    with pytest.raises(Forbidden) as caught:
        gateway_call(tools, "build_job", {"target": "game", "allow_network": True},
                     source="repl", gate="surface")
    assert caught.value.decision["tool"] == "build_network"
    assert caught.value.decision["call_id"]


def test_restore_is_a_mutation_graded_by_mode(tmp_path, ledger, mode):
    services = fake_services(tmp_path, tmp_path / "build")
    tools, _, _ = compose_facade(tmp_path, services)
    mode(pm.AUTO)
    job_id = output(gateway_call(tools, "build_fix", {"target": "game"}))["job_id"]
    mode(pm.MANUAL)
    with pytest.raises(Forbidden):
        gateway_call(tools, "build_fix_restore", {"job_id": job_id})
    mode(pm.ACCEPT_EDITS)
    assert gateway_call(tools, "build_fix_restore", {"job_id": job_id}).success
