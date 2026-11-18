"""An account's assigned API key authenticates chat as that account, until it is revoked.

Over the real ``serve.Handler``: a key minted by ``/v1/app-control/api-keys/generate``
is accepted by ``/v1/chat/completions`` as the account that owns it (its role and
tier reach the rate limit and metering), and is refused once revoked or once the
account is banned.  A key is never an app-control session: it cannot mint keys.
"""
from __future__ import annotations

import json

import pytest

import admin_auth
import sonder_runtime.interfaces.http.serve as ts
from tests import test_app_control_http as _wire
from tests.test_serve_auth import _http_server, _request

control = _wire.control

pytestmark = pytest.mark.unit

DEPLOYMENT_KEY = "deployment-key"
CHAT = json.dumps({"model": "sonder", "messages": [{"role": "user", "content": "hello"}]}).encode()


@pytest.fixture
def served(control, monkeypatch):
    binding, admin_token, _, open_db, _, _ = control
    monkeypatch.setattr(ts, "_APP_CONTROL_BINDING", binding)
    monkeypatch.setattr(ts, "API_KEY", DEPLOYMENT_KEY)
    monkeypatch.setattr(ts, "AUTH_MODE", "both")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    runtime = ts._legacy_runtime()
    monkeypatch.setattr(runtime, "_open_db", open_db)
    monkeypatch.setattr(runtime, "available_tiers", lambda: {})
    monkeypatch.setattr(runtime, "chat_web_response", lambda *args, **kwargs: None)
    monkeypatch.setattr(runtime, "answer_with_history", lambda *args, **kwargs: "keyed answer")
    metered = []
    real_meter = admin_auth.meter_usage

    def meter_spy(conn, account, tokens):
        metered.append(account)
        return real_meter(conn, account, tokens)

    monkeypatch.setattr(ts.admin_auth, "meter_usage", meter_spy)
    with _http_server(monkeypatch) as port:
        yield port, admin_token, open_db, metered


def _control(port, route, session, body=None):
    status, _, raw = _request(
        port, "POST", "/v1/app-control/" + route, body=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + DEPLOYMENT_KEY,
                 "X-Sonder-Account-Token": session},
    )
    return status, json.loads(raw)


def _chat(port, headers):
    status, _, raw = _request(port, "POST", "/v1/chat/completions", body=CHAT,
                              headers={"Content-Type": "application/json", **headers})
    return status, json.loads(raw)


def _both_mode(key):
    return {"Authorization": "Bearer " + DEPLOYMENT_KEY, "X-Sonder-Account-Token": key}


def test_a_generated_key_chats_as_its_account_until_revoked(served):
    port, admin_token, _, metered = served
    status, body = _control(port, "api-keys/generate", admin_token)
    assert status == 200 and body["key"].startswith(admin_auth.API_KEY_PREFIX)
    key, key_id = body["key"], body["key_id"]
    # Without an account credential this deployment refuses chat.
    assert _chat(port, {"Authorization": "Bearer " + DEPLOYMENT_KEY})[0] == 401

    status, body = _chat(port, _both_mode(key))
    assert status == 200, body
    assert body["choices"][0]["message"]["content"].startswith("keyed answer")
    # The key stands for its account: role and tier reach metering.
    assert metered and metered[-1]["username"] == "alice"
    assert metered[-1]["role"] == "admin" and metered[-1]["tier"]

    status, body = _control(port, "api-keys/revoke", admin_token, {"key_id": key_id})
    assert status == 200 and body["revoked"] == 1
    assert _chat(port, _both_mode(key))[0] == 401


def test_in_account_mode_the_key_alone_is_the_bearer(served, monkeypatch):
    port, admin_token, open_db, metered = served
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    conn = open_db()
    try:
        admin_auth.register(conn, "bob", "other-password")
        bob_key = admin_auth.generate_api_key(conn, "bob")["key"]
    finally:
        conn.close()

    status, body = _chat(port, {"Authorization": "Bearer " + bob_key})
    assert status == 200, body
    assert metered[-1]["username"] == "bob" and metered[-1]["role"] == "user"
    # A key that was never issued, or a near miss of a real one, is nobody.
    assert _chat(port, {"Authorization": "Bearer sk-" + "0" * 48})[0] == 401
    assert _chat(port, {"Authorization": "Bearer " + bob_key[:-1]})[0] == 401

    conn = open_db()
    try:
        conn.execute("UPDATE accounts SET banned=1 WHERE username='bob'")
        conn.commit()
    finally:
        conn.close()
    assert _chat(port, {"Authorization": "Bearer " + bob_key})[0] == 401


def test_a_key_is_not_an_app_control_session(served):
    port, admin_token, _, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    # A key cannot list or mint keys: key management needs a signed-in session.
    for route in ("api-keys/list", "api-keys/generate", "swarm/status"):
        status, body = _control(port, route, key)
        assert status == 401, (route, body)
    assert [row["key_id"] for row in _control(port, "api-keys/list", admin_token)[1]["keys"]] != []
