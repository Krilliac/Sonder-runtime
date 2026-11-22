from __future__ import annotations

import json

import pytest

import permission_modes as pm
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.interfaces.http.facades import approvals as facade
from tests.test_http_approvals import API_KEY, _http_server, _local_open, _request, _serve_with_accounts
from tests.test_permission_bypass_mode import _as_keyless_api_key, _chat
from tests.test_permission_bypass_mode import _restore_permission_state  # noqa: F401  (autouse fixture)

JSON = {"Content-Type": "application/json"}
REFUSED = "nobody is here"
Q = chr(39)


@pytest.fixture
def gate(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    pm.reset_unattended_for_tests()
    pm.forget_spent_approval()
    yield store
    pm.forget_spent_approval()
    pm.reset_unattended_for_tests()


def _line(marker):
    code = "open(r" + Q + str(marker) + Q + "," + Q + "w" + Q + ").write(" + Q + "x" + Q + ")"
    return "/run_code code=" + code + " language=python"


def _auth(monkeypatch, auth):
    if auth == "local-open":
        _local_open(monkeypatch)
    else:
        _as_keyless_api_key(monkeypatch)


def _get_mode(port, session="", headers=None):
    h = dict(headers or {})
    if session:
        h["X-Sonder-Session"] = session
    status, _, raw = _request(port, "GET", "/v1/permission-mode", headers=h)
    assert status == 200, raw
    return json.loads(raw)


@pytest.mark.parametrize("session", ["", "chat-1"])
@pytest.mark.parametrize("auth", ["local-open", "keyless-api-key"])
def test_local_open_http_cannot_approve_its_own_refused_call(gate, monkeypatch, tmp_path, auth, session):
    _auth(monkeypatch, auth)
    marker = tmp_path / "ran.txt"
    with _http_server(monkeypatch) as port:
        assert REFUSED in _chat(port, _line(marker), session)
        status, _, raw = _request(port, "GET", "/v1/approvals")
        assert status == 200
        pending = json.loads(raw)["pending"]
        assert len(pending) == 1 and pending[0]["tool"] == "run_code"
        call_id = pending[0]["call_id"]
        digest = gate.resolve_call(call_id).digest
        for ref, body, headers in (
            (call_id, "{}", JSON),
            (call_id, json.dumps({"ttl_seconds": 600}), JSON),
            (digest, "{}", JSON),
            (call_id, "{}", {**JSON, "Idempotency-Key": "k-1"}),
            (call_id, "{}", {**JSON, "Idempotency-Key": "k-1"}),
            (call_id, "{}", {**JSON, "Authorization": "Bearer nonsense"}),
            (call_id.upper(), "{}", JSON),
        ):
            status, _, raw = _request(port, "POST", "/v1/approvals/" + ref, body=body, headers=headers)
            assert status == 403, (ref, raw)
            error = json.loads(raw)["error"]
            assert error["code"] == "APPROVAL_REQUIRES_AUTHENTICATED_ADMIN", error
            assert "SONDER_API_KEY" in error["message"] and "SONDER_REQUIRE_ACCOUNT=1" in error["message"]
            assert "console" in error["message"]
        assert gate.approvals() == []
        assert REFUSED in _chat(port, _line(marker), session)
        assert not marker.exists()
        status, _, raw = _request(port, "POST", "/v1/approvals/deny/" + call_id, body="{}", headers=JSON)
        assert status == 200, raw
    assert gate.approvals() == []
    assert not marker.exists()


def test_local_open_http_can_still_revoke_a_console_approval(gate, monkeypatch):
    _local_open(monkeypatch)
    decision = pm.decide("file_write", interactive=False, surface="http",
                         arguments={"path": "n.txt", "content": "x", "mode": "create"})
    issued = facade.approve_call(gate, decision.call_id, {}, approver="console operator", surface="console")
    assert issued.status == 201
    nonce = issued.body["approval"]["nonce"]
    with _http_server(monkeypatch) as port:
        status, _, raw = _request(port, "POST", "/v1/approvals/revoke/" + nonce, body="{}", headers=JSON)
        assert status == 200, raw
        assert json.loads(raw)["approval"]["state"] == "revoked"


@pytest.mark.parametrize("scopes", [
    {"local-open|": "auto"},
    {"local-open|chat-1": "acceptEdits"},
    {"local-open|": "acceptEdits", "local-open|chat-1": "auto"},
    {"LOCAL-OPEN|": "auto"},
    {" local-open |chat-1": "auto"},
])
@pytest.mark.parametrize("auth", ["local-open", "keyless-api-key"])
def test_saved_local_open_scope_above_manual_loads_and_resolves_as_manual(
        gate, monkeypatch, tmp_path, scopes, auth):
    state = tmp_path / "permission_mode.json"
    state.write_text(json.dumps({"mode": "manual", "scopes": {**scopes, "api-key|s-9": "auto"}}),
                     encoding="utf-8")
    pm._reset_for_tests()
    _auth(monkeypatch, auth)
    marker = tmp_path / "ran.txt"
    with _http_server(monkeypatch) as port:
        for session in ("", "chat-1", "never-seen"):
            assert _get_mode(port, session)["mode"] == pm.MANUAL
            assert REFUSED in _chat(port, _line(marker), session)
            assert not marker.exists()
    assert all(v == pm.MANUAL for k, v in pm._SCOPED_MODES.items()
               if k.split("|", 1)[0].strip().lower() == "local-open")
    assert pm._SCOPED_MODES["api-key|s-9"] == pm.AUTO
    with pm.mode_scope("api-key", "s-9"):
        assert pm.current_mode() == pm.AUTO
    with pm._LOCK:
        pm._SCOPED_MODES["local-open|"] = pm.AUTO
    with pm.mode_scope("local-open", "x"):
        assert pm.current_mode() == pm.MANUAL
    pm.set_mode(pm.MANUAL)
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["scopes"]["local-open|"] == pm.MANUAL
    assert saved["scopes"]["api-key|s-9"] == pm.AUTO


@pytest.mark.parametrize("console_mode", ["acceptEdits", "auto", "bypass"])
@pytest.mark.parametrize("auth", ["local-open", "keyless-api-key"])
def test_console_unscoped_mode_is_not_inherited_by_local_open_http(
        gate, monkeypatch, tmp_path, console_mode, auth):
    with pm.attended_mode_change(), pm.attended_bypass_confirm():
        pm.set_mode(console_mode)
    assert pm.current_mode() == console_mode
    _auth(monkeypatch, auth)
    marker = tmp_path / "ran.txt"
    with _http_server(monkeypatch) as port:
        for session in ("", "chat-1"):
            assert _get_mode(port, session)["mode"] == pm.MANUAL
            assert REFUSED in _chat(port, _line(marker), session)
            assert not marker.exists()
    assert pm.current_mode() == console_mode


def test_plan_still_binds_local_open_http(gate, monkeypatch):
    pm.set_mode(pm.PLAN)
    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        assert _get_mode(port, "chat-1")["mode"] == pm.PLAN


def test_authenticated_api_key_scope_still_inherits_console_auto(gate, monkeypatch):
    with pm.attended_mode_change():
        pm.set_mode(pm.AUTO)
    _serve_with_accounts(monkeypatch, mode="api-key")
    with _http_server(monkeypatch) as port:
        assert _get_mode(port, "s-1", {"Authorization": "Bearer " + API_KEY})["mode"] == pm.AUTO


def test_account_token_presented_under_local_open_is_still_capped(gate, monkeypatch, tmp_path):
    with pm.attended_mode_change():
        pm.set_mode(pm.AUTO)
    with pm.mode_scope("account:ada", "s-1"):
        pm.set_mode(pm.AUTO)
    _serve_with_accounts(monkeypatch, mode="account")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    assert ts._effective_auth_mode() == "local-open"
    admin = {"Authorization": "Bearer admin-token"}
    decision = pm.decide("file_write", interactive=False, surface="http",
                         arguments={"path": "n.txt", "content": "x", "mode": "create"})
    with _http_server(monkeypatch) as port:
        assert _get_mode(port, "s-1", admin)["mode"] == pm.MANUAL
        assert _get_mode(port, "", admin)["mode"] == pm.MANUAL
        status, _, raw = _request(port, "POST", "/v1/approvals/" + decision.call_id, body="{}",
                                  headers={**JSON, **admin})
        assert status == 403 and json.loads(raw)["error"]["code"] == "APPROVAL_REQUIRES_AUTHENTICATED_ADMIN"


def test_storage_refuses_above_manual_in_a_local_open_scope(gate):
    for principal, kwargs in (("local-open", {}), ("account:ada", {"local_open": True})):
        with pm.mode_scope(principal, "s", **kwargs):
            for target in (pm.ACCEPT_EDITS, pm.AUTO, pm.BYPASS):
                with pytest.raises(ValueError):
                    pm.set_mode(target)
            assert pm.set_mode(pm.PLAN) == pm.PLAN
            assert pm.set_mode(pm.MANUAL) == pm.MANUAL
            assert pm.cycle_mode(-1) == pm.PLAN
            assert pm.cycle_mode(1) == pm.MANUAL
            with pytest.raises(ValueError):
                pm.cycle_mode(1)
            assert pm.current_mode() == pm.MANUAL
    assert not {pm.AUTO, pm.ACCEPT_EDITS, pm.BYPASS} & set(pm._SCOPED_MODES.values())


class _FakeHandler:
    def __init__(self, headers=None):
        self.headers = dict(headers or {})


@pytest.mark.parametrize("auth, headers, expected", [
    ("local-open", {}, "manual"),
    ("keyless-api-key", {}, "manual"),
    ("api-key", {"Authorization": "Bearer " + API_KEY}, "auto"),
    ("api-key", {}, "auto"),
])
def test_every_local_open_request_is_capped_without_a_mode_scope(gate, monkeypatch, auth, headers, expected):
    import contextvars

    with pm.attended_mode_change():
        pm.set_mode(pm.AUTO)
    if auth == "api-key":
        _serve_with_accounts(monkeypatch, mode="api-key")
    else:
        _auth(monkeypatch, auth)

    def handle():
        context = ts.Handler._request_auth_context(_FakeHandler(headers))
        decision = pm.decide("run_code", interactive=False, surface="http",
                             arguments={"code": "print(1)"}, record=False)
        return context, pm.current_mode(), decision.action

    context, mode, action = contextvars.copy_context().run(handle)
    if not headers and auth == "api-key":
        assert context["authorized"] is False
    assert mode == expected
    assert action == ("deny" if expected == "manual" else "allow")
    assert pm.current_mode() == pm.AUTO


def test_reset_and_rebind_clear_the_cap(gate):
    with pm.attended_mode_change():
        pm.set_mode(pm.AUTO)
    pm.bind_unauthenticated_caller(True)
    assert pm.current_mode() == pm.MANUAL
    with pm.mode_scope("api-key", "s"):
        assert pm.current_mode() == pm.MANUAL
    pm.bind_unauthenticated_caller(False)
    assert pm.current_mode() == pm.AUTO
