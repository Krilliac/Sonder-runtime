"""Direct-MCP permission preflight is a truthful read-only boundary."""
from __future__ import annotations

import json

import pytest

import permission_modes as pm
import permission_rules
import server

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def policy_sandbox(tmp_path, monkeypatch):
    monkeypatch.setattr(server.sonder_paths, "default_home", lambda: tmp_path)
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "mode.json"))
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = True
    permission_rules.reset_load_warnings()
    try:
        yield tmp_path
    finally:
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded
        permission_rules.reset_load_warnings()


def _write_rules(home, rules):
    (home / "permissions.json").write_text(json.dumps(rules), encoding="utf-8")


def test_preflight_uses_the_direct_mcp_gate_and_never_calls_the_target(
    policy_sandbox, monkeypatch,
):
    _write_rules(policy_sandbox, [{
        "pattern": "file_delete", "action": "deny", "note": "super-secret operator note",
    }])

    def target_must_not_run(*_args, **_kwargs):
        raise AssertionError("preflight executed file_delete")

    monkeypatch.setattr(server, "file_delete", target_must_not_run)
    out = server.policy_explain("/file_delete")

    assert "policy preflight: file_delete" in out
    assert "caller: direct MCP (non-interactive)" in out
    assert "gate result: deny" in out
    assert "explicit permission rule" in out
    assert "will execute: no" in out
    assert "super-secret" not in out


def test_preflight_reports_noninteractive_outcome_not_console_ask(policy_sandbox):
    pm.set_mode(pm.MANUAL)

    out = server.policy_explain("file_write")

    assert "mode: manual" in out
    assert "gate result: allow" in out
    assert "non-interactive caller policy" in out


def test_preflight_rejects_an_unknown_name_without_deciding(policy_sandbox, monkeypatch):
    monkeypatch.setattr(
        pm, "decide_for_caller",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("decided unknown tool")),
    )

    with pytest.raises(ValueError, match="unknown registered MCP tool"):
        server.policy_explain("definitely_not_a_registered_tool")


def test_preflight_names_the_existing_operator_control_exemption(policy_sandbox):
    pm.set_mode(pm.PLAN)

    out = server.policy_explain("permission_mode")

    assert "gate result: allow (operator control exemption)" in out
    assert "will execute: no" in out
