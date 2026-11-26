"""Tests for compact 2-tier agent help, admit-then-deny closure, project cwd decoupling, and admin tool-lane opt-in."""
from __future__ import annotations

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters import restricted_tool_lane as rtl
from sonder_runtime.adapters.security import role_tool_policy

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_permission_state():
    saved_state = dict(pm._STATE)
    saved_scopes = dict(pm._SCOPED_MODES)
    saved_loaded = pm._LOADED
    yield
    with pm._LOCK:
        pm._STATE.clear()
        pm._STATE.update(saved_state)
        pm._SCOPED_MODES.clear()
        pm._SCOPED_MODES.update(saved_scopes)
        pm._LOADED = saved_loaded


def test_agent_tool_help_compacts_for_2k_context_and_closes_admit_then_deny(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    compact = server._agent_tool_help(num_ctx=2048)
    assert len(compact) < 6000
    assert "other tools" in compact
    assert "tool_help" in compact

    full = server._agent_tool_help(num_ctx=32768)
    assert len(full) > len(compact) * 2

    # Plan mode must NOT advertise write/execution tools (admit-then-deny closed).
    pm.set_mode(pm.PLAN)
    plan_help = server._agent_tool_help(num_ctx=32768)
    assert "- file_write:" not in plan_help
    assert "- run_code:" not in plan_help
    assert "- file_read:" in plan_help

    # User role must NOT advertise tools outside user_tool_allowlist.json.
    pm.set_mode(pm.MANUAL)
    with role_tool_policy.caller_role_scope("user"):
        user_help = server._agent_tool_help(num_ctx=32768)
        assert "- run_code:" not in user_help


def test_cwd_decoupled_from_strict_project_tools(monkeypatch, tmp_path):
    dispatched = []
    prompts = []

    def fake_dispatch(name, args, **_kw):
        dispatched.append((name, args))
        return "ok"

    def fake_gen(prompt, history=None):
        prompts.append(prompt)
        if len(prompts) == 1:
            return '{"tool":"run_code","args":{"code":"print(1)"}}'
        return '{"final":"done"}'

    fake_gen.last_response_meta = {}
    monkeypatch.setattr(server, "_make_tier_generate", lambda *a, **k: fake_gen)
    monkeypatch.setattr(server, "_serve_target", lambda *a, **k: ("fixture-model", False, False, "code"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_agent_dispatch_observed", fake_dispatch)
    monkeypatch.setenv("SONDER_SPECULATION", "0")

    out = server._agent_impl(
        "run python snippet",
        project=str(tmp_path),
        strict_project_tools=False,
        max_steps=3,
    )
    assert "done" in out
    assert dispatched and dispatched[0][0] == "run_code"
    assert str(tmp_path.resolve()) in prompts[0]


def test_restricted_tool_lane_admin_opt_in_and_agent_approval_hook(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    pm.set_mode(pm.MANUAL)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: None)

    entry = rtl.entry("agent", str(tmp_path))
    with rtl.admin_opt_in(True):
        run, refusal = rtl.admit(
            entry, "astra-cyber-35b-a3b", "tool-lane-denied", None,
            unsafe=False, prepared=False,
            resolve=lambda: ("astra-cyber-35b-a3b", False, False, "model:astra-cyber-35b-a3b"),
            provider_for=lambda _l: None,
        )
    assert refusal == ""
    assert run is not None and run.admin_open is True

    # Interactive approval hook on _agent_permission_gate_error allows once when approved.
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    ledger = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: ledger)

    def hook(decision, tool_name, _args):
        ledger.issue(tool_name, ledger.resolve_call(decision.call_id).digest, approver="admin", surface="http")
        return "approved"

    with pm.agent_approval_hook(hook):
        err = server._agent_permission_gate_error("file_write", {"path": "x.txt", "content": "hi"})
    assert err == ""


def test_tool_help_handles_malformed_and_union_typed_schemas():
    from sonder_runtime.domain.agents.tool_help import filtered_schema, tool_help_text

    assert filtered_schema({"properties": None, "required": None}) == {
        "type": "object", "properties": {}, "required": [],
    }
    catalog = {
        "union_tool": {
            "description": "Accepts union-typed fields.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "target": {"type": ["string", "null"]},
                    "count": {"type": ["integer", "null"], "minimum": 3},
                },
                "required": ["target", "count"],
            },
        },
        "non_mapping_schema_tool": {
            "description": "Schema is unexpectedly a list.",
            "input_schema": ["not", "a", "mapping"],
        },
    }
    union_text = tool_help_text(name="union_tool", catalog=catalog)
    assert '"target":"<target>"' in union_text
    assert '"count":3' in union_text
    assert "target (required)" in union_text
    assert "count (required)" in union_text

    malformed_text = tool_help_text(name="non_mapping_schema_tool", catalog=catalog)
    assert "Arguments: none." in malformed_text
