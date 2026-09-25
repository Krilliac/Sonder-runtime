"""Permission grades for the developer tools, and approvals bound to the
host-resolved test command."""
from __future__ import annotations

import json

import pytest

import permission_modes as pm
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from tests.test_tools_test_runs_fakes import FakeTestRuns, native

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


def test_risk_grades():
    assert pm.risk_of("test_run") == "execution"
    for name in ("tool_inventory", "output_digest", "test_run_result"):
        assert pm.risk_of(name) == "safe", name


def test_the_execution_grade_is_what_the_gate_reads(monkeypatch):
    """Mutation proof: without test_run in EXECUTION_TOOLS the grade changes."""
    import command_catalog

    monkeypatch.setattr(pm, "EXECUTION_TOOLS", pm.EXECUTION_TOOLS - {"test_run"})
    command_catalog.reset_cache()  # the catalog memoises the grades it publishes
    try:
        assert pm.risk_of("test_run") != "execution"
    finally:
        monkeypatch.undo()
        command_catalog.reset_cache()
    assert pm.risk_of("test_run") == "execution"


def _call(arguments):
    return ("tools/call", {"name": "test_run", "arguments": arguments})


@pytest.mark.parametrize("mode", [pm.PLAN, pm.MANUAL, pm.ACCEPT_EDITS])
def test_plan_denies_and_manual_or_accept_edits_refuse_an_unattended_run(tmp_path, monkeypatch, ledger, mode):
    monkeypatch.setattr(pm, "current_mode", lambda: mode)
    replies, audit, developer = native(tmp_path, [_call({"runner": "pytest"})])
    result = replies[0]["result"]
    assert result["isError"] and result["error"] == "permission_denied"
    assert developer.test_runs.runs == []
    assert audit.read()[-1]["terminal"] == "policy_denied"


def test_auto_allows_it(tmp_path, monkeypatch, ledger):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    replies, _, developer = native(tmp_path, [_call({"runner": "pytest"})])
    assert replies[0]["result"]["isError"] is False
    assert len(developer.test_runs.runs) == 1


def test_a_one_shot_approval_is_bound_to_the_resolved_command(tmp_path, monkeypatch, ledger):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.MANUAL)
    arguments = {"runner": "pytest", "selector": "k:fast", "project": "."}
    plan = FakeTestRuns().plan(type("R", (), {"runner": "pytest", "selector": "k:fast",
                                              "project": "."})(), None)
    approved = {**arguments, "resolved_command": plan.resolved_command()}
    grant = ledger.issue("test_run", pm.call_digest("test_run", approved), approver="test")
    # an approval of the bare arguments (no resolved command) would not match
    bare = pm.call_digest("test_run", arguments)
    assert bare != pm.call_digest("test_run", approved)
    replies, _, developer = native(tmp_path, [
        _call({**arguments, "selector": "k:slow"}),   # a different command: refused
        _call(arguments),                               # the approved command: runs
        _call(arguments),                               # spent: refused again
    ])
    assert replies[0]["result"]["error"] == "permission_denied"
    assert replies[1]["result"]["isError"] is False, replies[1]
    assert replies[2]["result"]["error"] == "permission_denied"
    assert ledger.get(grant.nonce).spent
    assert [request.selector for request, _, _ in developer.test_runs.runs] == ["k:fast"]


def test_a_refused_plan_is_refused_before_anyone_is_asked(tmp_path, monkeypatch, ledger):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.AUTO)
    replies, audit, developer = native(tmp_path, [_call({"runner": "pytest", "selector": "a;b"})])
    result = replies[0]["result"]
    assert result["isError"] and result["error"] == "permission_denied"
    assert result["evidence"]["error_code"] == "INVALID_SELECTOR"
    assert ledger.pending() == []  # nothing was put in front of the operator
    assert developer.test_runs.runs == []


@pytest.mark.parametrize("tool, arguments", [
    ("tool_inventory", {}), ("output_digest", {"path": "build.log"}),
])
def test_safe_tools_run_in_plan_mode(tmp_path, monkeypatch, ledger, tool, arguments):
    monkeypatch.setattr(pm, "current_mode", lambda: pm.PLAN)
    replies, _, _ = native(tmp_path, [("tools/call", {"name": tool, "arguments": arguments})])
    assert replies[0]["result"]["isError"] is False, replies[0]
    json.loads(replies[0]["result"]["output"])
