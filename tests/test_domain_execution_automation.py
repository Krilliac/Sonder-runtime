"""SPEC-3 Phases 5-6: pure execution policy and automation state machines."""
from __future__ import annotations

import pytest

import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store
import sonder_runtime.adapters.persistence.fleet_store as fleet_store
import permission_rules
from sonder_runtime.domain.automation import state_machine as sm
from sonder_runtime.domain.execution import policy


# --- execution policy -------------------------------------------------------

def test_default_rules_deny_delete_and_allow_status():
    rules = policy.default_rules()
    assert policy.evaluate(rules, "file_delete")["action"] == "deny"
    assert policy.evaluate(rules, "status")["action"] == "allow"


def test_glob_first_match_wins():
    rules = policy.default_rules()
    assert policy.evaluate(rules, "run_code")["action"] == "ask"
    assert policy.evaluate(rules, "context_health")["action"] == "allow"


def test_unmatched_tool_defaults_to_ask():
    result = policy.evaluate(policy.default_rules(), "totally_unknown_tool")
    assert result["action"] == "ask"
    assert result["pattern"] == "*"


def test_normalize_rules_drops_invalid():
    raw = [
        {"pattern": "a", "action": "allow"},
        {"pattern": "", "action": "allow"},      # empty pattern
        {"pattern": "b", "action": "sideways"},  # invalid action
        "not a dict",
    ]
    normalized = policy.normalize_rules(raw)
    assert [r["pattern"] for r in normalized] == ["a"]


def test_upsert_rule_moves_to_front_and_dedupes():
    rules = policy.default_rules()
    updated = policy.upsert_rule(rules, "file_delete", "allow", "override")
    assert updated[0] == {"pattern": "file_delete", "action": "allow",
                          "note": "override"}
    assert sum(1 for r in updated if r["pattern"] == "file_delete") == 1


def test_upsert_rule_rejects_bad_input():
    with pytest.raises(ValueError):
        policy.upsert_rule([], "", "allow")
    with pytest.raises(ValueError):
        policy.upsert_rule([], "x", "maybe")


def test_permission_rules_delegates(tmp_path):
    # The root module's behavior is unchanged after delegation.
    home = str(tmp_path)
    assert permission_rules.check(home, "file_delete")["action"] == "deny"
    permission_rules.add_rule(home, "file_delete", "allow", "ok")
    assert permission_rules.check(home, "file_delete")["action"] == "allow"


# --- automation state machines ---------------------------------------------

def test_autopilot_status_sets_match_store():
    assert autopilot_store.ACTIVE_STATUSES == sm.AUTOPILOT_ACTIVE
    assert autopilot_store.TERMINAL_STATUSES == sm.AUTOPILOT_TERMINAL
    assert autopilot_store.ALL_STATUSES == sm.AUTOPILOT_ALL


def test_fleet_status_sets_match_store():
    assert fleet_store.ACTIVE_STATUSES == sm.FLEET_ACTIVE
    assert fleet_store.TERMINAL_STATUSES == sm.FLEET_TERMINAL


@pytest.mark.parametrize("current,nxt,ok", [
    ("running", "completed", True),
    ("running", "interrupted", True),
    ("paused", "running", True),
    ("interrupted", "running", True),   # explicit resume
    ("failed", "running", True),        # explicit retry
    ("completed", "running", False),    # terminal is terminal
    ("cancelled", "running", False),
    ("running", "ready", False),        # no going backward
])
def test_autopilot_transitions(current, nxt, ok):
    assert sm.autopilot_can_transition(current, nxt) is ok


def test_completed_and_cancelled_are_truly_terminal():
    # completed/cancelled have no successors; failed keeps an explicit-retry
    # edge even though the store groups it under TERMINAL_STATUSES.
    assert sm.autopilot_next_states("completed") == frozenset()
    assert sm.autopilot_next_states("cancelled") == frozenset()
    assert "running" in sm.autopilot_next_states("failed")
    for terminal in sm.AUTOPILOT_TERMINAL:
        assert sm.is_terminal(terminal)


@pytest.mark.parametrize("current,nxt,ok", [
    ("queued", "running", True),
    ("running", "done", True),
    ("running", "interrupted", True),
    ("interrupted", "queued", True),    # re-dispatch
    ("failed", "retried", True),
    ("done", "queued", False),          # terminal
    ("done", "running", False),
])
def test_fleet_transitions(current, nxt, ok):
    assert sm.fleet_can_transition(current, nxt) is ok


def test_unknown_status_never_transitions():
    assert sm.autopilot_can_transition("bogus", "running") is False
    assert sm.fleet_can_transition("bogus", "running") is False
