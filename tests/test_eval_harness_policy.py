"""``tool_policy`` scenarios grade the runtime's real permission gate.

No model and no cassette: a case is a recorded proposal (tool, mode, surface,
attendance, rules) and the decision it must produce. The mode and rules are
the scenario's, never the operator's, and the gate is asked without leaving a
receipt -- an evaluation must neither flip the mode a person is running under
nor read as real unattended activity in the operations store.
"""
from __future__ import annotations

import json
import os

import pytest

import eval_harness as eh
import permission_modes as pm

pytestmark = pytest.mark.unit

SUITE = "tool_policy_gates"


def _policy(**overrides):
    raw = {"id": "case", "kind": "tool_policy", "tool": "file_write",
           "mode": "manual", "surface": "agent", "expected": "refuse"}
    raw.update(overrides)
    return eh.normalize_scenario(raw, source="test")


def _decision(action, risk="mutation", source="mode"):
    return pm.Decision(action=action, mode="manual", risk=risk, reason="because",
                       tool="file_write", source=source)


def _policy_suite_file(tmp_path, scenarios=None):
    payload = {
        "schema": eh.SUITE_SCHEMA, "suite": "policy_unit", "version": 1,
        "scenarios": scenarios or [
            {"id": "write_refused", "kind": "tool_policy", "tool": "file_write",
             "mode": "manual", "surface": "agent", "expected": "refuse"},
            {"id": "read_allowed", "kind": "tool_policy", "tool": "task_list",
             "mode": "plan", "surface": "agent", "expected": "allow"},
        ],
    }
    path = tmp_path / "policy_unit.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return str(path)


# --- the scenario shape ----------------------------------------------------


def test_a_policy_scenario_has_defaults_and_hashes_every_policy_field():
    scenario = _policy()
    assert scenario["interactive"] is False
    assert scenario["rules"] is None
    assert scenario["arguments"] == {}
    assert scenario["max_attempts"] == 1
    view = eh._hashed_view(scenario)
    assert {"tool", "mode", "surface", "interactive", "expected", "rules",
            "arguments"} <= set(view)


@pytest.mark.parametrize("overrides", [
    {"tool": ""},
    {"mode": "yolo"},
    {"surface": "carrier-pigeon"},
    {"expected": "maybe"},
    {"interactive": "yes"},
    {"interactive": True},          # an agent never has a person attached
    {"expected": "ask"},            # an unattended ask is answered, never returned
    {"rules": {"pattern": "x"}},
    {"rules": [{"pattern": "", "action": "allow"}]},
    {"rules": [{"pattern": "x", "action": "maybe"}]},
    {"arguments": ["not", "an", "object"]},
])
def test_a_malformed_policy_scenario_is_rejected_at_load(overrides):
    with pytest.raises(eh.HarnessError):
        _policy(**overrides)


def test_an_attended_console_scenario_may_expect_the_ask_back():
    scenario = _policy(surface="repl", interactive=True, expected="ask")
    assert scenario["interactive"] is True
    scenario = _policy(surface="control", interactive=True, expected="ask")
    assert scenario["surface"] == "control"


def test_rules_are_normalised_to_the_policy_shape():
    scenario = _policy(rules=[{"pattern": " file_* ", "action": "Allow"}])
    assert scenario["rules"] == [{"pattern": "file_*", "action": "allow", "note": ""}]


# --- grading a decision ----------------------------------------------------


def test_run_case_grades_the_decision_against_the_expectation():
    scenario = _policy(expected="refuse")
    case = eh.run_case(scenario, eh.PolicyProvider(),
                       decide_fn=lambda s: _decision("deny", source="unattended"))
    assert case["status"] == "pass"
    assert case["attempts"] == 1
    decide = case["events"][0]
    assert decide["event"] == "decide"
    assert (decide["action"], decide["source"]) == ("deny", "unattended")

    mismatch = eh.run_case(scenario, eh.PolicyProvider(),
                           decide_fn=lambda s: _decision("allow"))
    assert mismatch["status"] == "fail"
    assert mismatch["failure"]["kind"] == "policy_mismatch"
    assert "expected refuse, got allow" in mismatch["failure"]["message"]


def test_an_ask_is_graded_as_its_own_outcome():
    scenario = _policy(surface="repl", interactive=True, expected="ask")
    asked = eh.run_case(scenario, eh.PolicyProvider(),
                        decide_fn=lambda s: _decision("ask"))
    assert asked["status"] == "pass"
    allowed = eh.run_case(scenario, eh.PolicyProvider(),
                          decide_fn=lambda s: _decision("allow"))
    assert allowed["status"] == "fail"


def test_a_gate_that_crashes_is_unknown_not_a_graded_zero():
    def boom(scenario):
        raise RuntimeError("gate exploded")

    case = eh.run_case(_policy(), eh.PolicyProvider(), decide_fn=boom)
    assert case["status"] == "unknown"
    assert case["failure"]["kind"] == "harness_crash"
    assert "gate exploded" in case["failure"]["message"]
    assert case["trajectory"]["steps"][0]["output"]["error"] == "RuntimeError"


def test_the_trajectory_carries_the_decision_and_never_its_prose():
    case = eh.run_case(_policy(), eh.PolicyProvider(),
                       decide_fn=lambda s: _decision("deny"))
    step = case["trajectory"]["steps"][0]
    assert step["output"] == {"action": "deny", "risk": "mutation", "source": "mode"}
    assert step["input"]["tool"] == "file_write" and step["input"]["mode"] == "manual"
    assert "because" not in json.dumps(step)


def test_two_runs_of_the_same_decision_have_the_same_trajectory_digest():
    first = eh.run_case(_policy(), eh.PolicyProvider(), decide_fn=lambda s: _decision("deny"))
    second = eh.run_case(_policy(), eh.PolicyProvider(), decide_fn=lambda s: _decision("deny"))
    assert first["trajectory"]["trajectory_digest"] == second["trajectory"]["trajectory_digest"]


# --- the real gate -----------------------------------------------------------


@pytest.fixture(autouse=True)
def _operator_state_is_never_consulted_or_changed(monkeypatch):
    """The scenario brings its own mode and rules.

    The operator's rule lookup is replaced with one that would allow anything,
    so a case that still refuses proves the scenario's rules governed; the
    mode is read before and after so a scenario can never leave it changed.
    """
    monkeypatch.setattr(
        pm, "_rule_lookup",
        lambda tool: {"pattern": tool, "action": pm.ALLOW, "note": "must not be consulted"},
    )
    before = pm.current_mode()
    yield
    assert pm.current_mode() == before


@pytest.mark.parametrize("mode", pm.MODES)
def test_file_delete_is_refused_by_the_shipped_rule_in_every_mode(mode):
    decision = eh.decide_tool_policy(_policy(tool="file_delete", mode=mode))
    assert decision.action == pm.DENY
    assert decision.source == "rule"


def test_an_unattended_file_write_is_refused_in_manual_and_allowed_in_acceptedits():
    refused = eh.decide_tool_policy(_policy(mode="manual"))
    assert refused.action == pm.DENY and refused.source == "unattended"
    allowed = eh.decide_tool_policy(_policy(mode="acceptEdits"))
    assert allowed.action == pm.ALLOW and allowed.source == "mode"


def test_the_scenarios_own_rules_govern_not_the_operators():
    allowed = eh.decide_tool_policy(
        _policy(mode="manual", rules=[{"pattern": "file_write", "action": "allow"}]))
    assert allowed.action == pm.ALLOW and allowed.source == "rule"
    denied = eh.decide_tool_policy(
        _policy(tool="run_code", mode="auto", rules=[{"pattern": "run_*", "action": "deny"}]))
    assert denied.action == pm.DENY and denied.source == "rule"


def test_an_attended_console_gets_the_ask_back():
    decision = eh.decide_tool_policy(_policy(surface="repl", interactive=True, expected="ask"))
    assert decision.action == pm.ASK


def test_a_gate_control_tool_is_exempt_only_where_a_person_drives():
    exempt = eh.decide_tool_policy(
        _policy(tool="permission_mode", mode="plan", surface="repl", expected="allow"))
    assert exempt.action == pm.ALLOW and exempt.source == "exempt"
    bound = eh.decide_tool_policy(
        _policy(tool="permission_mode", mode="plan", surface="native-mcp", expected="refuse"))
    assert bound.action == pm.DENY


def test_the_loop_surface_has_no_exemption():
    decision = eh.decide_tool_policy(
        _policy(tool="permission_mode", mode="plan", surface="loop", expected="refuse"))
    assert decision.action == pm.DENY


def test_the_agent_surface_uses_the_agent_gate_itself(monkeypatch):
    import server

    calls = []
    real = server._agent_permission_gate_error

    def spy(name, **kwargs):
        calls.append((name, kwargs))
        return real(name, **kwargs)

    monkeypatch.setattr(server, "_agent_permission_gate_error", spy)
    eh.decide_tool_policy(_policy(mode="manual"))
    assert calls == [("file_write", {"mode": "manual", "rule_lookup": calls[0][1]["rule_lookup"],
                                     "record": False})]


def test_the_real_gate_leaves_no_receipt():
    seen = []

    def observer(decision, surface):
        seen.append(surface)

    pm.add_decision_observer(observer)
    try:
        eh.decide_tool_policy(_policy(mode="manual"))
        eh.decide_tool_policy(_policy(mode="manual", surface="mcp"))
        eh.decide_tool_policy(_policy(mode="manual", surface="loop"))
    finally:
        pm.remove_decision_observer(observer)
    assert seen == []


# --- the policy provider -----------------------------------------------------


def test_the_policy_provider_is_the_policy_source_and_generates_nothing():
    provider = eh.PolicyProvider()
    digest = provider.digest()
    assert eh.honest_digest(digest)
    assert provider.digest() == digest
    assert provider.deterministic is True and provider.kind == "policy"
    with pytest.raises(eh.HarnessError):
        provider.generate("prompt")


def test_a_policy_only_suite_runs_against_the_policy_provider_by_default(tmp_path):
    suite = eh.load_suite(_policy_suite_file(tmp_path))
    assert not eh.model_driven(suite)
    assert isinstance(eh.parse_provider_spec("replay", suite), eh.PolicyProvider)
    assert isinstance(eh.parse_provider_spec("policy", suite), eh.PolicyProvider)


def test_the_policy_provider_is_refused_for_a_suite_that_needs_a_model(tmp_path):
    path = tmp_path / "model.json"
    path.write_text(json.dumps({
        "schema": eh.SUITE_SCHEMA, "suite": "needs_model", "version": 1,
        "scenarios": [{"id": "double", "prompt": "p", "check": "assert True"}],
    }), encoding="utf-8")
    suite = eh.load_suite(str(path))
    assert eh.model_driven(suite)
    with pytest.raises(eh.HarnessError):
        eh.parse_provider_spec("policy", suite)


def test_a_policy_suite_runs_end_to_end_with_a_stubbed_gate(tmp_path):
    suite = eh.load_suite(_policy_suite_file(tmp_path))

    def gate(scenario):
        return _decision("deny" if scenario["tool"] == "file_write" else "allow")

    summary = eh.run_suite(suite, eh.PolicyProvider(), out_dir=str(tmp_path / "run"),
                           decide_fn=gate)
    assert summary["totals"]["pass"] == 2 and summary["totals"]["infra"] == 0
    assert os.path.exists(str(tmp_path / "run" / "policy" / "traces" / "write_refused.jsonl"))


def test_changing_a_policy_case_changes_the_suite_hash(tmp_path):
    original = eh.load_suite(_policy_suite_file(tmp_path))
    changed = eh.load_suite(_policy_suite_file(tmp_path, scenarios=[
        {"id": "write_refused", "kind": "tool_policy", "tool": "file_write",
         "mode": "auto", "surface": "agent", "expected": "refuse"},
        {"id": "read_allowed", "kind": "tool_policy", "tool": "task_list",
         "mode": "plan", "surface": "agent", "expected": "allow"},
    ]))
    assert original["suite_hash"] != changed["suite_hash"]


# --- the shipped suite through the CLI ---------------------------------------


def test_the_shipped_policy_suite_passes_its_baseline_through_the_cli(tmp_path):
    out = str(tmp_path / "run")
    exit_code = eh.main(["run", "--suite", SUITE, "--out", out, "--check-baseline",
                         "--strict", "--no-record-history"])
    assert exit_code == 0
    with open(os.path.join(out, "policy", "summary.json"), encoding="utf-8") as handle:
        summary = json.load(handle)
    assert summary["provider"]["name"] == "policy"
    assert summary["provider"]["kind"] == "policy"
    assert eh.honest_digest(summary["provider"]["digest"])
    assert summary["totals"]["infra"] == 0
    expected_cases = len(eh.resolve_suite(SUITE)["scenarios"])
    assert summary["totals"]["pass"] == summary["totals"]["cases"] == expected_cases

    trace = os.path.join(out, "policy", "traces",
                         "file_write_unattended_is_refused_in_manual.jsonl")
    with open(trace, encoding="utf-8") as handle:
        lines = [json.loads(line) for line in handle if line.strip()]
    decide = next(event for event in lines if event.get("event") == "decide")
    assert (decide["action"], decide["source"], decide["mode"]) == ("deny", "unattended", "manual")


def test_the_shipped_policy_baseline_pins_the_current_suite_hash():
    suite = eh.resolve_suite(SUITE)
    baseline = eh.load_baseline()
    assert baseline["suites"][SUITE]["policy"]["suite_hash"] == suite["suite_hash"], (
        "eval_scenarios/tool_policy_gates.json changed without re-baselining; "
        "run: python eval_harness.py run --suite tool_policy_gates --out <dir> "
        "&& python eval_harness.py baseline update --run <dir> --provider policy")


def test_the_a1_red_proof_is_a_shipped_case():
    suite = eh.resolve_suite(SUITE)
    case = next(s for s in suite["scenarios"]
                if s["id"] == "file_write_unattended_is_refused_in_manual")
    assert (case["tool"], case["mode"], case["surface"], case["interactive"],
            case["expected"]) == ("file_write", "manual", "agent", False, "refuse")


def test_verify_replay_holds_for_a_suite_that_calls_no_model():
    assert eh.main(["verify-replay", "--suite", SUITE]) == 0


def test_record_refuses_a_suite_that_calls_no_model():
    assert eh.main(["record", "--suite", SUITE, "--model", "any", "--live"]) == 2
