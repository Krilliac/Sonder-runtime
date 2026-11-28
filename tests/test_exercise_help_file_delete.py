"""file_delete stays advertised to agents; shipped ask and custom deny rules enforce at call time.

Regression: ``server._agent_tool_help`` hid every tool whose permission
decision was a ``rule`` deny, so any ``deny`` rule silently removed a
registered, dispatchable tool from the agent's help with no entry in
``dispatch.excluded_names``. Visibility is not permission: rules are enforced by
the call-time gate, and the classifier still asks for every delete.
"""
from __future__ import annotations

import permission_modes as pm
import permission_rules
import pytest
import server

from sonder_runtime.bootstrap import generic_agent_dispatch as dispatch
from sonder_runtime.domain.security import tool_call_classifier as classifier

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_permission_state(monkeypatch, tmp_path):
    # permission_modes reads its state file through _state_path(); patch it so
    # set_mode(PLAN) below never persists into the shared test home.
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    saved_state = dict(pm._STATE)
    saved_scopes = dict(pm._SCOPED_MODES)
    saved_loaded = pm._LOADED
    pm._reset_for_tests()
    yield
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved_state)
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved_scopes)
        pm._LOADED = saved_loaded


def _advertised(**flags):
    return set(server._agent_help_advertised_tools(server._agent_tool_help(**flags)))


def test_file_delete_is_registered_not_excluded_and_blocked_by_shipped_policy():
    registered = {tool.name for tool in server.mcp._tool_manager.list_tools()}
    assert "file_delete" in registered
    assert "file_delete" not in dispatch.excluded_names(server.mcp, server._AGENT_SYSTEM_OPERATOR_TOOLS)
    decision = pm.decide("file_delete", interactive=False, record=False)
    assert not decision.allowed
    assert decision.source == "unattended"


@pytest.mark.parametrize("flags", [{}, {"allow_location": True}])
def test_file_delete_is_advertised_under_shipped_ask_and_custom_deny_rules(monkeypatch, flags):
    assert "file_delete" in _advertised(**flags)
    monkeypatch.setattr(
        pm,
        "_rule_lookup",
        permission_rules.rule_lookup([{"pattern": "file_delete", "action": "deny", "reason": "custom deny"}]),
    )
    decision = pm.decide("file_delete", interactive=False, record=False)
    assert not decision.allowed and decision.source == "rule"
    assert "file_delete" in _advertised(**flags)


def test_file_delete_call_time_gate_refuses_under_shipped_policy_and_custom_deny(monkeypatch):
    refusal = server._agent_permission_gate_error(
        "file_delete", {"path": "scratch.txt", "dry_run": True}, record=False)
    assert refusal and "file_delete" in refusal
    monkeypatch.setattr(
        pm,
        "_rule_lookup",
        permission_rules.rule_lookup([{"pattern": "file_delete", "action": "deny", "reason": "custom deny"}]),
    )
    deny_refusal = server._agent_permission_gate_error(
        "file_delete", {"path": "scratch.txt", "dry_run": True}, record=False)
    assert deny_refusal and "file_delete" in deny_refusal


def test_file_delete_dispatched_by_an_agent_is_refused_and_deletes_nothing(monkeypatch, tmp_path):
    victim = tmp_path / "keep.txt"
    victim.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(
        pm,
        "_rule_lookup",
        permission_rules.rule_lookup([{"pattern": "file_delete", "action": "deny", "reason": "custom deny"}]),
    )
    with server._managed_agent_admission_scope():
        out = server._agent_dispatch("file_delete", {"path": str(victim)})
    assert isinstance(out, str) and out.startswith("ERROR"), out
    assert victim.read_text(encoding="utf-8") == "keep"


def test_file_delete_always_asks_in_classifier():
    assert classifier.classify("file_delete", {"path": "scratch.txt"}, risk="dangerous") == classifier.ASK
    assert classifier.classify("file_delete", {"path": "scratch.txt"}, risk="safe") == classifier.ASK


def test_plan_mode_deny_still_hides_file_delete_behind_a_custom_rule_deny(monkeypatch):
    # A rule deny is decided before the mode; it must not mask plan's own deny
    # and let plan advertise a destructive tool.
    monkeypatch.setattr(
        pm,
        "_rule_lookup",
        permission_rules.rule_lookup([{"pattern": "file_delete", "action": "deny", "reason": "custom deny"}]),
    )
    pm.set_mode(pm.PLAN)
    assert pm.decide("file_delete", interactive=False, record=False).source == "rule"
    advertised = _advertised()
    assert "file_delete" not in advertised
    assert "file_write" not in advertised
    assert "file_read" in advertised
