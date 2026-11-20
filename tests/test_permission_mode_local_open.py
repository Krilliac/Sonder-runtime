"""A local-open listener must not let a credential-less caller raise autonomy.

Under ``local-open`` (the loopback default, and ``api-key`` with no key
configured on a loopback bind) ``_admin_authorized`` is true for every local
process. ``POST /v1/permission-mode`` checked only that, so any local process
with no secret could set ``auto`` -- where execution tools are ALLOW
unattended -- and then run host code through chat ``/run_code``. The mode is
also saved to ``permission_mode.json``, so the raise outlived the process.

These tests drive the real handler over a real loopback socket.
"""
from __future__ import annotations

import json
import threading
from contextlib import contextmanager
import http.client

import pytest

import permission_modes as pm
import server
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.execution_tools import code_runner

pytestmark = pytest.mark.unit

ESCALATION_CODE = "PERMISSION_MODE_ESCALATION_REFUSED"
API_KEY = "test-owner-key-0123456789abcdef"


@pytest.fixture(autouse=True)
def mode_sandbox(tmp_path, monkeypatch):
    """A tmp state file, the manual default, and no on-disk rules."""
    state = tmp_path / "permission_mode.json"
    monkeypatch.setattr(pm, "_state_path", lambda: str(state))
    monkeypatch.setattr(pm, "_rule_lookup", lambda _tool: None)
    saved = dict(pm._STATE)
    saved_loaded = pm._LOADED
    with pm._LOCK:
        pm._STATE.update(mode=pm.DEFAULT_MODE, elevated=False, elevation_reason="")
    pm._LOADED = True
    try:
        yield state
    finally:
        with pm._LOCK:
            pm._STATE.clear()
            pm._STATE.update(saved)
        pm._LOADED = saved_loaded


@pytest.fixture(autouse=True)
def _no_model_discovery(monkeypatch, _configure_http_legacy_boundary):
    monkeypatch.setattr(ts._legacy_runtime(), "discovered_model_records", lambda: [])
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)


def _local_open(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)


def _api_key(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", API_KEY)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)


@contextmanager
def _http_server(monkeypatch):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    ts._live_telemetry_application(build=True)
    warm_operations = getattr(ts.sonder_lifecycle.get(), "operations", None)
    if callable(warm_operations):
        warm_operations()
    ts.command_catalog.http_slash_tools()
    ts.command_catalog.catalog()
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _request(port, method, path, body=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def _post_mode(port, mode, headers=None):
    status, payload = _request(
        port, "POST", "/v1/permission-mode", body=json.dumps({"mode": mode}),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    return status, json.loads(payload)


def _chat(port, content, headers=None):
    body = json.dumps({"model": "sonder", "messages": [{"role": "user", "content": content}]})
    status, payload = _request(
        port, "POST", "/v1/chat/completions", body=body,
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    assert status == 200, payload
    return json.loads(payload)["choices"][0]["message"]["content"]


def _saved_mode(state):
    if not state.exists():
        return None
    return json.loads(state.read_text(encoding="utf-8"))["mode"]


# --- engine primitive -------------------------------------------------------


@pytest.mark.parametrize("current,target", [
    (pm.MANUAL, pm.AUTO), (pm.MANUAL, pm.ACCEPT_EDITS),
    (pm.ACCEPT_EDITS, pm.AUTO), (pm.PLAN, pm.AUTO), (pm.PLAN, pm.ACCEPT_EDITS),
])
def test_set_mode_without_escalation_refuses_a_raise_and_saves_nothing(
        mode_sandbox, current, target):
    with pm._LOCK:
        pm._STATE["mode"] = current
    with pytest.raises(pm.ModeEscalationRefused) as refused:
        pm.set_mode_without_escalation(target)
    assert (refused.value.current, refused.value.target) == (current, target)
    assert isinstance(refused.value, ValueError)
    assert pm.current_mode() == current
    assert not mode_sandbox.exists()


@pytest.mark.parametrize("current,target", [
    (pm.AUTO, pm.MANUAL), (pm.AUTO, pm.ACCEPT_EDITS), (pm.AUTO, pm.PLAN),
    (pm.ACCEPT_EDITS, pm.MANUAL), (pm.PLAN, pm.MANUAL), (pm.MANUAL, pm.PLAN),
    (pm.AUTO, pm.AUTO), (pm.MANUAL, pm.MANUAL),
])
def test_set_mode_without_escalation_still_lowers_and_persists(mode_sandbox, current, target):
    with pm._LOCK:
        pm._STATE["mode"] = current
    assert pm.set_mode_without_escalation(target) == target
    assert pm.current_mode() == target
    assert _saved_mode(mode_sandbox) == target


def test_set_mode_without_escalation_rejects_an_unknown_mode():
    with pytest.raises(ValueError) as error:
        pm.set_mode_without_escalation("turbo")
    assert not isinstance(error.value, pm.ModeEscalationRefused)
    assert pm.current_mode() == pm.MANUAL


def test_set_mode_without_escalation_ignores_the_console_attended_context(mode_sandbox):
    """The HTTP caller decided it holds no attended authority; a stray context
    must not turn the refusal back off."""
    with pm.attended_mode_change():
        with pytest.raises(pm.ModeEscalationRefused):
            pm.set_mode_without_escalation(pm.AUTO)
    assert pm.current_mode() == pm.MANUAL


def test_provider_exposes_the_non_escalating_setter(mode_sandbox):
    provider = ts.permission_policy
    assert provider.mode_escalation_refused() is pm.ModeEscalationRefused
    with pytest.raises(provider.mode_escalation_refused()):
        provider.set_mode_without_escalation("auto")
    assert provider.set_mode_without_escalation("plan") == pm.PLAN


# --- the HTTP endpoint -------------------------------------------------------


@pytest.mark.parametrize("target", ["auto", "acceptEdits", "AUTO", "acc"])
def test_local_open_post_cannot_raise_the_mode(monkeypatch, mode_sandbox, target):
    _local_open(monkeypatch)
    with _http_server(monkeypatch) as port:
        status, payload = _post_mode(port, target)
        assert status == 403, payload
        assert payload["error"]["code"] == ESCALATION_CODE
        assert "local-open" in payload["error"]["message"]
        assert payload["error"]["retryable"] is False
        assert payload["mode"] == pm.MANUAL
        get_status, get_payload = _request(port, "GET", "/v1/permission-mode")
        assert get_status == 200
        assert json.loads(get_payload)["mode"] == pm.MANUAL
    assert pm.current_mode() == pm.MANUAL
    assert not mode_sandbox.exists(), "a refused raise must not be saved"


def test_local_open_raise_from_accept_edits_to_auto_is_refused(monkeypatch, mode_sandbox):
    _local_open(monkeypatch)
    pm.set_mode(pm.ACCEPT_EDITS)  # as the console would
    with _http_server(monkeypatch) as port:
        status, payload = _post_mode(port, "auto")
    assert status == 403, payload
    assert payload["error"]["code"] == ESCALATION_CODE
    assert payload["mode"] == pm.ACCEPT_EDITS
    assert pm.current_mode() == pm.ACCEPT_EDITS
    assert _saved_mode(mode_sandbox) == pm.ACCEPT_EDITS


def test_local_open_refusal_with_idempotency_key_is_still_403(monkeypatch, mode_sandbox):
    _local_open(monkeypatch)
    monkeypatch.setattr(ts.served_action_receipts, "claim", lambda *a, **k: "claimed")
    finished = []
    monkeypatch.setattr(ts.served_action_receipts, "finish",
                        lambda *a, **k: finished.append(k))
    with _http_server(monkeypatch) as port:
        for _ in range(2):
            status, payload = _post_mode(port, "auto", {"Idempotency-Key": "raise-once"})
            assert status == 403, payload
            assert payload["error"]["code"] == ESCALATION_CODE
    # Nothing changed, so the receipt is a completed refusal, never "uncertain".
    assert finished and all(not k.get("uncertain") for k in finished)
    assert pm.current_mode() == pm.MANUAL
    assert not mode_sandbox.exists()


@pytest.mark.parametrize("target", ["manual", "plan", "acceptEdits"])
def test_local_open_post_may_still_lower_the_mode(monkeypatch, mode_sandbox, target):
    _local_open(monkeypatch)
    pm.set_mode(pm.AUTO)  # raised at the console
    with _http_server(monkeypatch) as port:
        status, payload = _post_mode(port, target)
    assert status == 200, payload
    assert payload["mode"] == target
    assert pm.current_mode() == target
    assert _saved_mode(mode_sandbox) == target


def test_local_open_post_may_return_from_plan_to_manual(monkeypatch, mode_sandbox):
    _local_open(monkeypatch)
    pm.set_mode(pm.PLAN)
    with _http_server(monkeypatch) as port:
        status, payload = _post_mode(port, "manual")
    assert status == 200, payload
    assert pm.current_mode() == pm.MANUAL


def test_authenticated_admin_may_still_raise_the_mode(monkeypatch, mode_sandbox):
    _api_key(monkeypatch)
    with _http_server(monkeypatch) as port:
        status, payload = _post_mode(port, "auto", {"Authorization": "Bearer " + API_KEY})
        assert status == 200, payload
        assert payload["mode"] == pm.AUTO
        # Without the key the same request is unauthenticated, not escalated.
        status, _payload = _post_mode(port, "manual")
        assert status == 401
    assert pm.current_mode() == pm.AUTO
    assert _saved_mode(mode_sandbox) == pm.AUTO


# --- end to end: the raise that made chat /run_code run host code -------------


def test_local_open_cannot_raise_then_run_code_through_chat(monkeypatch, mode_sandbox):
    """The reported chain: POST auto with no secret, then chat ``/run_code``."""
    _local_open(monkeypatch)
    ran = []

    def runner(*args, **kwargs):
        ran.append((args, kwargs))
        return {"ok": True, "returncode": 0, "stdout": "42", "stderr": "",
                "language": "python"}

    monkeypatch.setattr(code_runner, "run_code", runner)
    with _http_server(monkeypatch) as port:
        status, payload = _post_mode(port, "auto")
        reply = _chat(port, "/run_code print(6*7)")
    # Checked first so the pre-fix failure names the harm, not the status.
    assert ran == [], "host code ran after a credential-less mode raise: %r" % reply
    assert status == 403, payload
    assert payload["error"]["code"] == ESCALATION_CODE
    assert reply.startswith("refused /run_code:"), reply
    assert "manual" in reply
    assert pm.current_mode() == pm.MANUAL
    assert not mode_sandbox.exists()


def test_admin_raise_still_lets_chat_run_code(monkeypatch, mode_sandbox):
    """The attended raise keeps working end to end for an authenticated admin."""
    _api_key(monkeypatch)
    ran = []

    def runner(*args, **kwargs):
        ran.append((args, kwargs))
        return {"ok": True, "returncode": 0, "stdout": "42", "stderr": "",
                "language": "python"}

    monkeypatch.setattr(code_runner, "run_code", runner)
    auth = {"Authorization": "Bearer " + API_KEY}
    with _http_server(monkeypatch) as port:
        status, payload = _post_mode(port, "auto", auth)
        assert status == 200, payload
        reply = _chat(port, "/run_code print(6*7)", auth)
    assert len(ran) == 1, reply
    assert "refused" not in reply
    assert server.permission_mode_data()["mode"] == pm.AUTO


# --- what the refusal tells the operator, and failing closed -----------------


@pytest.mark.parametrize("headers", [
    {},
    {"Authorization": "Bearer admin-token"},
    {"X-Sonder-Account-Token": "admin-token"},
], ids=["no-credential", "admin-token-as-bearer", "admin-token-as-account-header"])
def test_local_open_refusal_names_the_settings_that_restore_the_raise(
        monkeypatch, mode_sandbox, headers):
    """An admin account token does not lift local-open: the effective auth
    mode stays local-open, so the message must name the server settings."""
    _local_open(monkeypatch)
    monkeypatch.setattr(
        ts, "_auth_account",
        lambda header: {"username": "ada", "role": "admin"}
        if "admin-token" in str(header or "") else None)
    with _http_server(monkeypatch) as port:
        status, payload = _post_mode(port, "auto", headers)
    assert status == 403, payload
    message = payload["error"]["message"]
    assert payload["error"]["code"] == ESCALATION_CODE
    assert "SONDER_API_KEY" in message and "SONDER_REQUIRE_ACCOUNT=1" in message
    assert "/mode auto" in message
    assert pm.current_mode() == pm.MANUAL
    assert not mode_sandbox.exists()


@pytest.mark.parametrize("context", [
    None, {}, {"mode": ""}, {"mode": "open"}, {"mode": "local-open"}, "local-open",
])
def test_mode_applier_fails_closed_without_an_authenticated_context(mode_sandbox, context):
    from sonder_runtime.interfaces.http.facades import permission_mode_post

    result = permission_mode_post.mode_applier(ts.permission_policy, context, "auto")()
    assert isinstance(result, pm.ModeEscalationRefused), result
    assert pm.current_mode() == pm.MANUAL
    assert not mode_sandbox.exists()


@pytest.mark.parametrize("mode", ["api-key", "account", "both", "either"])
def test_mode_applier_keeps_the_attended_raise_for_authenticated_modes(mode_sandbox, mode):
    from sonder_runtime.interfaces.http.facades import permission_mode_post

    assert permission_mode_post.mode_applier(
        ts.permission_policy, {"mode": mode}, "auto")() == pm.AUTO
    assert pm.current_mode() == pm.AUTO


def test_unattended_refusal_does_not_send_callers_to_a_local_open_post(mode_sandbox):
    """The MCP/chat refusal names POST /v1/permission-mode; under local-open
    that POST refuses a raise, so the text must say what it needs."""
    text = pm.unattended_escalation_refusal("auto")
    assert text.startswith("refused")
    assert "POST /v1/permission-mode" in text
    assert "SONDER_API_KEY" in text and "SONDER_REQUIRE_ACCOUNT=1" in text
    assert "local-open" in text
