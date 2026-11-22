"""Tests for Mode.BYPASS across permission_modes, file_ops, HTTP, REPL, and audit."""
from __future__ import annotations

import json
import os
import threading
import time

import pytest

import permission_modes as pm
import server
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.security import permission_receipts, role_tool_policy
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.interfaces.repl import repl as sonder_repl
from tests.test_http_approvals import API_KEY, _http_server, _local_open, _request, _serve_with_accounts

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
    for risk in ("safe", "ask", "mutation", "execution", "dangerous"):
        assert pm._MATRIX[pm.BYPASS][risk] == pm.ALLOW
    # Unclassified tools stay guarded even in bypass.
    assert pm._MATRIX[pm.BYPASS][pm.UNCLASSIFIED] == pm.ASK

    rules = {
        "file_delete": {"pattern": "file_delete", "action": "deny", "note": "destructive by default"},
        "admin_private_chain_of_thought": {
            "pattern": "admin_private_chain_of_thought",
            "action": "deny",
            "note": "cot guard",
        },
    }
    lookup = lambda name: rules.get(name)  # noqa: E731
    del_decision = pm.decide("file_delete", interactive=False, mode=pm.BYPASS, rule_lookup=lookup)
    assert del_decision.action == pm.DENY and del_decision.source == "rule"

    # Even without a deny rule, delete-always-ask (L16) holds in BYPASS too.
    no_rule = lambda _n: None  # noqa: E731
    assert pm.decide("file_delete", interactive=True, mode=pm.BYPASS, rule_lookup=no_rule).action == pm.ASK
    assert pm.decide("file_delete", interactive=False, mode=pm.BYPASS, rule_lookup=no_rule).action == pm.DENY
    assert pm.decide(
        "run_code", interactive=False, mode=pm.BYPASS, rule_lookup=no_rule,
        arguments={"language": "python", "code": "import os; os.remove('notes.txt')"},
    ).action == pm.DENY

    cot_decision = pm.decide(
        "admin_private_chain_of_thought", interactive=False, mode=pm.BYPASS, rule_lookup=lookup
    )
    assert cot_decision.action == pm.DENY

    for tool in pm.DURABLE_AUTHORITY_TOOLS:
        assert pm.decide(tool, interactive=False, mode=pm.BYPASS, rule_lookup=lambda _n: None).action == pm.DENY
        assert pm.decide(tool, interactive=True, mode=pm.BYPASS, rule_lookup=lambda _n: None).action == pm.ASK


def test_bypass_holds_process_wide_cloud_consent():
    """A scoped bypass never turns on cloud egress for every caller unprompted."""
    assert pm.DURABLE_AUTHORITY_TOOLS < pm.BYPASS_HELD_TOOLS
    assert "cloud_opt_in" in pm.BYPASS_HELD_TOOLS
    none = lambda _n: None  # noqa: E731
    unattended = pm.decide("cloud_opt_in", interactive=False, mode=pm.BYPASS, rule_lookup=none)
    assert unattended.action == pm.DENY and unattended.source == "durable-authority"
    assert pm.decide("cloud_opt_in", interactive=True, mode=pm.BYPASS, rule_lookup=none).action == pm.ASK
    # Enabling cloud_opt_in unattended is still refused even with an explicit allow rule.
    allow = lambda _n: {"pattern": "cloud_opt_in", "action": pm.ALLOW, "note": "operator"}  # noqa: E731
    assert pm.decide(
        "cloud_opt_in", interactive=False, mode=pm.BYPASS, rule_lookup=allow,
        arguments={"action": "on"},
    ).action == pm.DENY
    assert pm.decide(
        "cloud_opt_in", interactive=False, mode=pm.BYPASS, rule_lookup=allow,
        arguments={"action": "status"},
    ).action == pm.ALLOW


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
        with pytest.raises(ValueError):
            pm.set_mode(pm.BYPASS)
        assert file_ops.bypass_enabled() is False

    with pm.mode_scope("api-key", "sess-a"):
        pm.set_mode(pm.BYPASS)
        assert file_ops.bypass_enabled() is True
        assert file_ops.resolve_path(str(target)) == target.resolve()

        with role_tool_policy.caller_role_scope("user"):
            assert file_ops.bypass_enabled() is False
            with pytest.raises(PermissionError):
                file_ops.resolve_path(str(target))

    for principal in ("api-key", "local-open"):
        with pm.mode_scope(principal, "sess-b"):
            assert pm.current_mode() == pm.DEFAULT_MODE
            assert file_ops.bypass_enabled() is False


def _capture_mode_audits(monkeypatch):
    audits = []
    monkeypatch.setattr(
        permission_receipts,
        "emit_mode_transition",
        lambda prev, curr, **kw: audits.append((prev, curr, kw)),
    )
    return audits


def _assert_http_bypass_refused(port, audits):
    """Every spelling of an HTTP bypass request is a 403 and changes nothing."""
    for body, headers in (
        ({"mode": "bypass", "confirm_bypass": True}, {**JSON, "X-Sonder-Session": "chat-1"}),
        ({"mode": "bypass", "confirm_bypass": True}, JSON),
        ({"mode": "bypass", "confirm_bypass": True, "session_id": "chat-2"}, JSON),
        ({"mode": "bypass"}, JSON),
        ({"mode": "BYPASS", "confirm_bypass": True}, JSON),
    ):
        status, _, raw = _request(
            port, "POST", "/v1/permission-mode", body=json.dumps(body), headers=headers,
        )
        assert status == 403, (body, raw)
        payload = json.loads(raw)
        assert payload["code"] == "BYPASS_REQUIRES_AUTHENTICATED_ADMIN"
        assert "local-open" in payload["error"] and "console" in payload["error"]

    status_get, _, body_get = _request(port, "GET", "/v1/permission-mode")
    assert status_get == 200
    assert json.loads(body_get)["can_bypass"] is False
    for session in ("", "chat-1", "chat-2"):
        headers = {"X-Sonder-Session": session} if session else {}
        status, _, raw = _request(port, "GET", "/v1/permission-mode", headers=headers)
        assert status == 200
        assert json.loads(raw)["mode"] == pm.DEFAULT_MODE, session
    assert pm.BYPASS not in pm._SCOPED_MODES.values()
    assert audits == []


def test_local_open_http_cannot_enter_bypass(monkeypatch, tmp_path):
    """Under local-open every local process is an unauthenticated "admin" and
    they all share the principal ``local-open``; ``confirm_bypass`` is a JSON
    field any of them can send, so it cannot stand in for a person or an
    account."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    audits = _capture_mode_audits(monkeypatch)
    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        _assert_http_bypass_refused(port, audits)
        # Ordinary mode changes still work under local-open.
        status, _, raw = _request(
            port, "POST", "/v1/permission-mode",
            body=json.dumps({"mode": "plan"}), headers=JSON,
        )
        assert status == 200, raw
        assert json.loads(raw)["mode"] == pm.PLAN
        assert json.loads(raw)["can_bypass"] is False


def test_keyless_api_key_on_loopback_is_local_open_and_cannot_enter_bypass(monkeypatch, tmp_path):
    """``api-key`` with no key on a loopback bind is downgraded to local-open at
    configuration time, and the downgraded server refuses HTTP bypass too."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    audits = _capture_mode_audits(monkeypatch)
    # The globals ``configure_typed_config`` leaves for that deployment.
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "HOST", "127.0.0.1")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    assert ts._effective_auth_mode() == "local-open"
    with _http_server(monkeypatch) as port:
        _assert_http_bypass_refused(port, audits)
    # ...and that is exactly what its downgrade yields for api-key with no key.
    assert ts._keyless_loopback_auth_mode("api-key", "", "127.0.0.1") == "local-open"
    assert ts._keyless_loopback_auth_mode("api-key", "", "::1") == "local-open"
    assert ts._keyless_loopback_auth_mode("api-key", "", "localhost") == "local-open"
    assert ts._keyless_loopback_auth_mode("api-key", "k" * 40, "127.0.0.1") == "api-key"
    assert ts._keyless_loopback_auth_mode("api-key", "", "0.0.0.0") == "api-key"


@pytest.mark.parametrize("auth_mode", ["account", "both"])
def test_admin_account_http_bypass_gated_scoped_and_audited(monkeypatch, tmp_path, auth_mode):
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    audits = _capture_mode_audits(monkeypatch)
    _serve_with_accounts(monkeypatch, mode=auth_mode)

    def auth(token):
        if auth_mode == "both":
            return {"Authorization": "Bearer " + API_KEY, "X-Sonder-Account-Token": token}
        return {"Authorization": "Bearer " + token}

    with _http_server(monkeypatch) as port:
        status_get, _, body_get = _request(port, "GET", "/v1/permission-mode", headers=auth("admin-token"))
        assert status_get == 200
        assert json.loads(body_get)["can_bypass"] is True

        status_unconfirmed, _, body_unconfirmed = _request(
            port, "POST", "/v1/permission-mode",
            body=json.dumps({"mode": "bypass"}), headers={**JSON, **auth("admin-token")},
        )
        assert status_unconfirmed == 400
        assert json.loads(body_unconfirmed)["code"] == "BYPASS_CONFIRMATION_REQUIRED"

        headers_sess = {**JSON, **auth("admin-token"), "X-Sonder-Session": "chat-1"}
        status_ok, _, body_ok = _request(
            port, "POST", "/v1/permission-mode",
            body=json.dumps({"mode": "bypass", "confirm_bypass": True}),
            headers=headers_sess,
        )
        assert status_ok == 200, body_ok
        assert json.loads(body_ok)["mode"] == "bypass"
        assert json.loads(body_ok)["can_bypass"] is True

        status_other, _, body_other = _request(
            port, "GET", "/v1/permission-mode",
            headers={**auth("admin-token"), "X-Sonder-Session": "chat-2"},
        )
        assert status_other == 200
        assert json.loads(body_other)["mode"] == pm.DEFAULT_MODE

        status_user, _, _ = _request(
            port, "POST", "/v1/permission-mode",
            body=json.dumps({"mode": "bypass", "confirm_bypass": True}),
            headers={**JSON, **auth("user-token")},
        )
        assert status_user == 403

    assert audits and audits[-1][:2] == (pm.DEFAULT_MODE, pm.BYPASS)
    assert audits[-1][2]["confirmed"] is True


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
        assert pm.current_mode() == pm.BYPASS
    # A scoped bypass lives for this process only; it is never written down.
    assert json.loads(state_file.read_text(encoding="utf-8")) == {"mode": pm.AUTO}
    with pm.mode_scope("dev-1", "s-2"):
        pm.set_mode(pm.PLAN)
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    assert raw == {"mode": pm.AUTO, "scopes": {"dev-1|s-2": pm.PLAN}}

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


@pytest.mark.parametrize("scope_key", ["local-open|", "local-open|chat-1", "account:ada|s-1"])
def test_saved_scoped_bypass_loads_as_manual(monkeypatch, tmp_path, scope_key):
    """A state file holding a scoped bypass (an older build wrote them, or a
    hand edit) restores that scope to the default, never to bypass."""
    state_file = tmp_path / "permission_mode.json"
    state_file.write_text(json.dumps({
        "mode": pm.MANUAL,
        "scopes": {scope_key: pm.BYPASS, "other|s-9": pm.AUTO},
    }), encoding="utf-8")
    monkeypatch.setattr(pm, "_state_path", lambda: str(state_file))
    pm._reset_for_tests()
    principal, _, session = scope_key.partition("|")
    with pm.mode_scope(principal, session):
        assert pm.current_mode() == pm.MANUAL
        assert pm.bypass_root_containment_active() is False
    with pm.mode_scope("other", "s-9"):
        assert pm.current_mode() == pm.AUTO
    assert scope_key not in pm._SCOPED_MODES
    # The next save rewrites the file without the dropped entry.
    with pm.mode_scope("other", "s-9"):
        pm.set_mode(pm.ACCEPT_EDITS)
    raw = json.loads(state_file.read_text(encoding="utf-8"))
    assert raw == {"mode": pm.MANUAL, "scopes": {"other|s-9": pm.ACCEPT_EDITS}}


# --- cloud_opt_in: consent only a person may give ---------------------------


_ENABLING = ({"action": "on"}, {"action": " ON "}, {"action": "enable"},
             {"action": "Enabled"}, {"action": "1"}, {"action": "true"},
             {"action": "yes"}, {"action": "bogus"}, None)


def _allow_everything(_tool):
    return {"pattern": "*", "action": pm.ALLOW, "note": "test"}


@pytest.mark.parametrize("mode", list(pm.MODES))
@pytest.mark.parametrize("rules", ["no-rule", "allow-rule"])
def test_unattended_cloud_opt_in_enabling_refused_in_every_mode(mode, rules):
    lookup = _allow_everything if rules == "allow-rule" else (lambda _n: None)
    for arguments in _ENABLING:
        decision = pm.decide("cloud_opt_in", interactive=False, mode=mode,
                             rule_lookup=lookup, arguments=arguments, record=False)
        assert decision.action == pm.DENY, (mode, rules, arguments, decision)
        assert decision.source == "durable-authority"
        assert "SONDER_ALLOW_CLOUD" in decision.reason


def test_cloud_opt_in_status_and_off_keep_their_ordinary_grading():
    """Only the enabling form is held: status reads and off revokes."""
    for arguments in ({}, {"action": "status"}, {"action": "off"}, {"action": "0"},
                      {"action": ""}):
        assert pm.enables_unattended_consent("cloud_opt_in", arguments) is False
        bypass = pm.decide("cloud_opt_in", interactive=False, mode=pm.BYPASS,
                           rule_lookup=lambda _n: None, arguments=arguments, record=False)
        assert bypass.action == pm.ALLOW, (arguments, bypass)
        allowed = pm.decide("cloud_opt_in", interactive=False, mode=pm.MANUAL,
                            rule_lookup=_allow_everything, arguments=arguments, record=False)
        assert allowed.action == pm.ALLOW, (arguments, allowed)
    # The console, with a person present, is still asked in manual.
    attended = pm.decide("cloud_opt_in", interactive=True, mode=pm.MANUAL,
                         rule_lookup=lambda _n: None, arguments={"action": "on"}, record=False)
    assert attended.action == pm.ASK


@pytest.fixture
def _chat_gate(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / "approvals.db")
    monkeypatch.setattr(pm, "_approval_ledger", lambda: store)
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    pm.reset_unattended_for_tests()
    pm.forget_spent_approval()
    # setenv first so teardown removes whatever the call under test leaves.
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "0")
    monkeypatch.delenv("SONDER_ALLOW_CLOUD")
    yield store
    pm.forget_spent_approval()
    pm.reset_unattended_for_tests()


def _chat(port, content, session, headers=None):
    body = {"model": "sonder", "messages": [{"role": "user", "content": content}]}
    if session:
        body["session"] = session
    status, _, raw = _request(port, "POST", "/v1/chat/completions",
                              body=json.dumps(body), headers={**JSON, **(headers or {})})
    assert status == 200, raw
    return json.loads(raw)["choices"][0]["message"]["content"]


@pytest.mark.parametrize("session", ["", "chat-1"])
@pytest.mark.parametrize("rules", ["no-rule", "allow-rule"])
def test_scoped_bypass_cannot_turn_on_cloud_over_http_chat(
        _chat_gate, monkeypatch, session, rules):
    """A scoped bypass (set before this fix, or at the console) plus
    ``/cloud_opt_in on`` from an unauthenticated local process must not flip
    process-wide cloud consent, and an allow rule must not reopen it."""
    if rules == "allow-rule":
        monkeypatch.setattr(pm, "_rule_lookup", _allow_everything)
    with pm.mode_scope("local-open", session):
        with pytest.raises(ValueError):
            pm.set_mode(pm.BYPASS)
        assert pm.current_mode() == pm.MANUAL
    with pm.mode_scope("api-key", session):
        pm.set_mode(pm.BYPASS)
        assert pm.current_mode() == pm.BYPASS
    _serve_with_accounts(monkeypatch, mode="api-key")
    key = {"Authorization": "Bearer " + API_KEY}
    with _http_server(monkeypatch) as port:
        # Positive control: the turn really runs under the scoped bypass, so a
        # status read (dangerous-graded, refused unattended in manual with no
        # rule) runs.
        status_text = _chat(port, "/cloud_opt_in status", session, key)
        assert status_text.startswith("hosted/cloud tiers: disabled"), status_text
        for line in ("/cloud_opt_in on", "/cloud_opt_in action=on", "/cloud_opt_in enable"):
            text = _chat(port, line, session, key)
            assert text.startswith("refused /cloud_opt_in"), (line, text)
            assert "SONDER_ALLOW_CLOUD" in text, (line, text)
            assert "SONDER_ALLOW_CLOUD" not in os.environ, (line, text)
        off_text = _chat(port, "/cloud_opt_in off", session, key)
        assert off_text.startswith("hosted/cloud tiers: disabled"), off_text


def test_allow_rule_cannot_turn_on_cloud_over_local_open_http_chat(_chat_gate, monkeypatch):
    """No bypass at all: manual mode with an allow rule for every tool."""
    monkeypatch.setattr(pm, "_rule_lookup", _allow_everything)
    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        status_text = _chat(port, "/cloud_opt_in status", "")
        assert status_text.startswith("hosted/cloud tiers: disabled"), status_text
        text = _chat(port, "/cloud_opt_in on", "")
        assert text.startswith("refused /cloud_opt_in"), text
        assert "SONDER_ALLOW_CLOUD" not in os.environ, text


# --- cycle 2: local-open cannot raise the mode above manual over HTTP --------


def _post_mode(port, mode, session=""):
    headers = {**JSON, "X-Sonder-Session": session} if session else JSON
    status, _, raw = _request(
        port, "POST", "/v1/permission-mode",
        body=json.dumps({"mode": mode}), headers=headers,
    )
    return status, json.loads(raw)


def _as_keyless_api_key(monkeypatch):
    """The globals ``configure_typed_config`` leaves for api-key with no key on loopback."""
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "HOST", "127.0.0.1")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "AUTH_MODE", ts._keyless_loopback_auth_mode("api-key", "", "127.0.0.1"))
    assert ts._effective_auth_mode() == "local-open"


@pytest.mark.parametrize("session", ["", "chat-1"])
@pytest.mark.parametrize("auth", ["local-open", "keyless-api-key"])
def test_local_open_http_cannot_raise_mode_above_manual_to_run_host_code(
        _chat_gate, monkeypatch, tmp_path, auth, session):
    """The reviewer's exploit: an unauthenticated local process POSTs ``auto``
    (whose matrix ALLOWs execution unattended) and then runs ``/run_code`` with
    nobody present. Every mode above manual must be refused, the mode must not
    move, and the code must still be refused afterwards."""
    if auth == "local-open":
        _local_open(monkeypatch)
    else:
        _as_keyless_api_key(monkeypatch)
    marker = tmp_path / "ran.txt"
    line = "/run_code code=open(r'%s','w').write('x') language=python" % marker
    with _http_server(monkeypatch) as port:
        before = _chat(port, line, session)
        assert "nobody is here" in before, before
        assert not marker.exists()

        for wanted in ("auto", "acceptEdits", "AUTO", "accept-edits", "au"):
            status, payload = _post_mode(port, wanted, session)
            assert status == 403, (wanted, payload)
            assert payload["code"] == "ESCALATION_REQUIRES_AUTHENTICATED_ADMIN", payload
            assert "local-open" in payload["error"] and "/mode " in payload["error"]
        status_get, _, raw_get = _request(
            port, "GET", "/v1/permission-mode",
            headers={"X-Sonder-Session": session} if session else {},
        )
        assert status_get == 200
        assert json.loads(raw_get)["mode"] == pm.DEFAULT_MODE

        after = _chat(port, line, session)
        assert "nobody is here" in after, after
        assert not marker.exists(), after

        # Lowering, and plan back to manual, stay open under local-open.
        for wanted in (pm.PLAN, pm.MANUAL):
            status, payload = _post_mode(port, wanted, session)
            assert status == 200, (wanted, payload)
            assert payload["mode"] == wanted
            assert payload["can_bypass"] is False
    assert not {pm.AUTO, pm.ACCEPT_EDITS, pm.BYPASS} & set(pm._SCOPED_MODES.values())
    assert pm._STATE["mode"] not in (pm.AUTO, pm.ACCEPT_EDITS, pm.BYPASS)


@pytest.mark.parametrize("auth_mode", ["account", "both", "api-key"])
def test_authenticated_http_callers_can_still_raise_mode(monkeypatch, tmp_path, auth_mode):
    """Authenticated admins and the owner API key keep today's behaviour."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    _serve_with_accounts(monkeypatch, mode=auth_mode)
    if auth_mode == "account":
        auth = {"Authorization": "Bearer admin-token"}
    elif auth_mode == "both":
        auth = {"Authorization": "Bearer " + API_KEY, "X-Sonder-Account-Token": "admin-token"}
    else:
        auth = {"Authorization": "Bearer " + API_KEY}
    with _http_server(monkeypatch) as port:
        for wanted in (pm.ACCEPT_EDITS, pm.AUTO, pm.MANUAL):
            status, _, raw = _request(
                port, "POST", "/v1/permission-mode",
                body=json.dumps({"mode": wanted}),
                headers={**JSON, **auth, "X-Sonder-Session": "s-1"},
            )
            assert status == 200, (wanted, raw)
            assert json.loads(raw)["mode"] == wanted


def test_unattended_escalation_refusal_does_not_send_local_open_callers_to_the_post(
        monkeypatch, tmp_path):
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    pm.set_mode(pm.MANUAL)
    for target in (pm.AUTO, pm.ACCEPT_EDITS):
        text = pm.unattended_escalation_refusal(target)
        assert text.startswith("refused"), text
        assert "/mode %s" % target in text
        assert "authenticated administrator" in text
        assert "local-open" in text


# --- cycle 2: reading and revoking cloud consent is never blocked ------------


@pytest.mark.parametrize("mode", list(pm.MODES))
def test_piped_console_cloud_off_and_status_pass_the_gate(monkeypatch, tmp_path, mode):
    """``sonder < script`` with an allow rule: ``/cloud off`` / ``/cloud status``
    (and a bare ``/cloud``, which runs status) are graded as what they are, never
    as consent; ``/cloud on`` is refused as consent in every mode. ``plan``
    blocks the dangerous-graded tool outright, by mode, whatever the rule; that
    grading predates this branch and is not the consent hold."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    monkeypatch.setattr(pm, "_rule_lookup", _allow_everything)
    monkeypatch.setattr(sonder_repl, "_console_has_operator", lambda: False)
    pm._reset_for_tests()
    pm.set_mode(mode)
    for argument in ("off", "status", "", " OFF ", "disable"):
        may_run, refusal = sonder_repl._named_command_gate("/cloud", argument)
        assert "SONDER_ALLOW_CLOUD" not in refusal, (mode, argument, refusal)
        if mode == pm.PLAN:
            assert may_run is False and "plan mode" in refusal, (argument, refusal)
        else:
            assert (may_run, refusal) == (True, ""), (mode, argument, refusal)
    for argument in ("on", "enable", " ON "):
        may_run, refusal = sonder_repl._named_command_gate("/cloud", argument)
        assert may_run is False, (mode, argument)
        assert "SONDER_ALLOW_CLOUD" in refusal, (mode, argument, refusal)


def test_policy_explain_cloud_opt_in_reports_the_status_form(monkeypatch, tmp_path):
    """With no arguments the gate assumes the enabling form; the preflight must
    not report ``status`` as refused, and must say what it preflighted."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    monkeypatch.setattr(pm, "_rule_lookup", _allow_everything)
    pm._reset_for_tests()
    pm.set_mode(pm.MANUAL)
    out = server.policy_explain("cloud_opt_in")
    assert "gate result: allow" in out, out
    assert "arguments: action=status" in out, out
    assert "attended Sonder console" in out, out
    monkeypatch.setattr(pm, "_rule_lookup", lambda _n: {"pattern": "*", "action": pm.DENY, "note": ""})
    assert "gate result: deny" in server.policy_explain("cloud_opt_in")
    # Other tools are unchanged: no arguments line.
    assert "arguments:" not in server.policy_explain("file_write")


def test_console_global_bypass_never_leaks_to_other_http_routes(monkeypatch, tmp_path):
    """Console global bypass must never leak into non-chat HTTP routes (e.g. POST /v1/build/jobs)."""
    monkeypatch.setattr(pm, "_state_path", lambda: str(tmp_path / "permission_mode.json"))
    pm._reset_for_tests()
    with pm.attended_mode_change(), pm.attended_bypass_confirm():
        pm.set_mode(pm.BYPASS)
    assert pm.current_mode() == pm.BYPASS

    _serve_with_accounts(monkeypatch, mode="account")
    observed = {}

    def _capture_build(self, method, path, req=None):
        if method != "POST" or path != "/v1/build/jobs":
            return False
        observed["post"] = {
            "mode": pm.current_mode(),
            "bypass_root": pm.bypass_root_containment_active(),
            "file_ops_bypass": file_ops.bypass_enabled(),
        }
        self._send_json_payload(observed["post"], status=200)
        return True

    def _capture_commands(context=None):
        observed["get"] = {
            "mode": pm.current_mode(),
            "bypass_root": pm.bypass_root_containment_active(),
            "file_ops_bypass": file_ops.bypass_enabled(),
        }
        return observed["get"]

    monkeypatch.setattr(ts.Handler, "_handle_build_request", _capture_build)
    monkeypatch.setattr(ts, "_commands_index_payload", _capture_commands)

    dev_headers = {"Content-Type": "application/json", "Authorization": "Bearer dev-token"}
    with _http_server(monkeypatch) as port:
        post_status, _, post_body = _request(
            port, "POST", "/v1/build/jobs", body="{}", headers=dev_headers,
        )
        get_status, _, get_body = _request(
            port, "GET", "/v1/commands", headers={"Authorization": "Bearer dev-token"},
        )

    assert post_status == 200, post_body
    assert json.loads(post_body) == {
        "mode": pm.MANUAL,
        "bypass_root": False,
        "file_ops_bypass": False,
    }
    assert get_status == 200, get_body
    assert json.loads(get_body) == {
        "mode": pm.MANUAL,
        "bypass_root": False,
        "file_ops_bypass": False,
    }
    # Console global bypass remains intact outside the HTTP request scopes.
    assert pm.current_mode() == pm.BYPASS
