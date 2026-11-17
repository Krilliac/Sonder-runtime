"""Tests for Mode.BYPASS across permission_modes, file_ops, HTTP, REPL, and audit."""
from __future__ import annotations

import json
import threading
import time

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.security import permission_receipts, role_tool_policy
from sonder_runtime.interfaces.repl import repl as sonder_repl
from tests.test_http_approvals import _http_server, _local_open, _request, _serve_with_accounts

pytestmark = pytest.mark.unit

JSON = {"Content-Type": "application/json"}


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


def test_bypass_matrix_and_protected_boundaries(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    assert pm.BYPASS == "bypass"
    assert pm.BYPASS in pm.MODES
    for risk in ("safe", "ask", "mutation", "execution", "dangerous", pm.UNCLASSIFIED):
        assert pm._MATRIX[pm.BYPASS][risk] == pm.ALLOW

    rules = {
        "file_delete": {"pattern": "file_delete", "action": "deny", "note": "shipped default"},
        "admin_private_chain_of_thought": {
            "pattern": "admin_private_chain_of_thought",
            "action": "deny",
            "note": "cot guard",
        },
    }
    lookup = lambda name: rules.get(name)  # noqa: E731
    del_decision = pm.decide("file_delete", interactive=False, mode=pm.BYPASS, rule_lookup=lookup)
    assert del_decision.allowed and del_decision.source == "mode:bypass"

    cot_decision = pm.decide(
        "admin_private_chain_of_thought", interactive=False, mode=pm.BYPASS, rule_lookup=lookup
    )
    assert cot_decision.action == pm.DENY

    for tool in pm.DURABLE_AUTHORITY_TOOLS:
        assert pm.decide(tool, interactive=False, mode=pm.BYPASS, rule_lookup=lambda _n: None).action == pm.DENY
        assert pm.decide(tool, interactive=True, mode=pm.BYPASS, rule_lookup=lambda _n: None).action == pm.ASK


def test_bypass_file_ops_containment_keeps_user_role_blocked(monkeypatch, tmp_path):
    state_file = tmp_path / "permission_mode.json"
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(state_file))
    pm._reset_for_tests()
    allowed_dir = tmp_path / "allowed"
    outside_dir = tmp_path / "outside"
    allowed_dir.mkdir()
    outside_dir.mkdir()
    target = outside_dir / "note.txt"
    target.write_text("hi", encoding="utf-8")
    monkeypatch.setattr(file_ops, "allowed_roots", lambda _extra="": [allowed_dir.resolve()])

    with pytest.raises(PermissionError):
        file_ops.resolve_path(str(target))

    with pm.mode_scope("local-open", "sess-a"):
        pm.set_mode(pm.BYPASS)
        assert file_ops.bypass_enabled() is True
        assert file_ops.resolve_path(str(target)) == target.resolve()

        with role_tool_policy.caller_role_scope("user"):
            assert file_ops.bypass_enabled() is False
            with pytest.raises(PermissionError):
                file_ops.resolve_path(str(target))

    with pm.mode_scope("local-open", "sess-b"):
        assert pm.current_mode() == pm.DEFAULT_MODE
        assert file_ops.bypass_enabled() is False


def test_http_permission_mode_bypass_gated_scoped_and_audited(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    audits = []
    monkeypatch.setattr(
        permission_receipts,
        "emit_mode_transition",
        lambda prev, curr, **kw: audits.append((prev, curr, kw)),
    )

    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        status_get, _, body_get = _request(port, "GET", "/v1/permission-mode")
        assert status_get == 200
        assert json.loads(body_get)["can_bypass"] is True

        status_unconfirmed, _, body_unconfirmed = _request(
            port, "POST", "/v1/permission-mode",
            body=json.dumps({"mode": "bypass"}), headers=JSON,
        )
        assert status_unconfirmed == 400
        assert json.loads(body_unconfirmed)["code"] == "BYPASS_CONFIRMATION_REQUIRED"

        headers_sess = {**JSON, "X-Sonder-Session": "chat-1"}
        status_ok, _, body_ok = _request(
            port, "POST", "/v1/permission-mode",
            body=json.dumps({"mode": "bypass", "confirm_bypass": True}),
            headers=headers_sess,
        )
        assert status_ok == 200, body_ok
        assert json.loads(body_ok)["mode"] == "bypass"

        status_other, _, body_other = _request(
            port, "GET", "/v1/permission-mode",
            headers={"X-Sonder-Session": "chat-2"},
        )
        assert status_other == 200
        assert json.loads(body_other)["mode"] == pm.DEFAULT_MODE

    assert audits and audits[-1][:2] == (pm.DEFAULT_MODE, pm.BYPASS)
    assert audits[-1][2]["confirmed"] is True

    _serve_with_accounts(monkeypatch)
    with _http_server(monkeypatch) as port:
        status_user, _, _ = _request(
            port, "POST", "/v1/permission-mode",
            body=json.dumps({"mode": "bypass", "confirm_bypass": True}),
            headers={**JSON, "Authorization": "Bearer user-token"},
        )
        assert status_user == 403


def test_unattended_mcp_permission_mode_bypass_refused(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    reply = server.permission_mode("bypass")
    assert reply.startswith("refused:") and "bypass" in reply
    assert pm.current_mode() == pm.DEFAULT_MODE


def test_repl_mode_bypass_requires_yes_confirmation(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_PERMISSION_STATE", str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr("builtins.input", lambda _prompt: "no")
    res_no = sonder_repl._mode_command("bypass")
    assert "cancelled" in res_no.lower()
    assert pm.current_mode() == pm.DEFAULT_MODE

    monkeypatch.setattr("builtins.input", lambda _prompt: "yes")
    res_yes = sonder_repl._mode_command("bypass")
    assert "bypass" in res_yes.lower()
    assert pm.current_mode() == pm.BYPASS


def test_scoped_mode_persistence_keeps_default_shape_when_empty(monkeypatch, tmp_path):
    state_file = tmp_path / "permission_mode.json"
    monkeypatch.setattr(pm, "_state_path", lambda: str(state_file))
    pm._reset_for_tests()
    pm.set_mode(pm.AUTO)
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"mode": pm.AUTO}

    with pm.mode_scope("dev-1", "s-1"):
        pm.set_mode(pm.BYPASS)
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    assert raw["mode"] == pm.AUTO
    assert raw["scopes"]["dev-1|s-1"] == pm.BYPASS

    # Ensure concurrent _SCOPED_MODES reads remain thread-safe under scope churn.
    errors = []

    def worker(idx: int) -> None:
        try:
            with pm.mode_scope("p", f"s-{idx}"):
                pm.set_mode(pm.PLAN if idx % 2 else pm.AUTO)
                assert pm.current_mode() in pm.MODES
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)
    assert not errors
    assert pm.mode_scope is not None
    assert time.time() > 0
