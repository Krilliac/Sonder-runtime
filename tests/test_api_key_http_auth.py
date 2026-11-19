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
from sonder_runtime.bootstrap import chat_tools_wiring
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


def _with_db(open_db, action):
    conn = open_db()
    try:
        return action(conn)
    finally:
        conn.close()


def _admin_account_edit(port, headers):
    body = json.dumps({"username": "alice", "tier": "pro"}).encode()
    status, _, raw = _request(port, "POST", "/v1/sonder/admin/account", body=body,
                              headers={"Content-Type": "application/json", **headers})
    return status, raw


def test_an_admin_accounts_key_has_no_admin_authority(served, monkeypatch):
    port, admin_token, _, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    # A leaked key of an admin account is not an admin credential: admin needs
    # a signed-in session or the deployment key.
    keyed = ts._auth_context("Bearer " + DEPLOYMENT_KEY, key)
    assert keyed["authorized"] and keyed["account"]["role"] == "admin"
    assert ts._admin_authorized(keyed) is False
    assert keyed["account"]["via_api_key"] is True
    session = ts._auth_context("Bearer " + DEPLOYMENT_KEY, admin_token)
    assert ts._admin_authorized(session) is True and not session["account"].get("via_api_key")

    assert _admin_account_edit(port, _both_mode(key))[0] in (401, 403)
    status, _, _ = _request(port, "POST", "/v1/admin/drain", body=b"{}",
                            headers={"Content-Type": "application/json", **_both_mode(key)})
    assert status in (401, 403)
    # The same route still serves the admin's signed-in session.
    status, raw = _admin_account_edit(port, _both_mode(admin_token))
    assert status == 200, raw

    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    assert ts._admin_authorized(ts._auth_context("Bearer " + key)) is False
    assert _admin_account_edit(port, {"Authorization": "Bearer " + key})[0] in (401, 403)


def test_a_developer_key_runs_chat_tools_and_a_user_key_does_not(served, monkeypatch):
    port, _, open_db, _ = served
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")

    def mint(conn):
        admin_auth.register(conn, "dev", "dev-password", role="developer", trusted_local=True)
        admin_auth.register(conn, "flagged", "flag-password", trusted_local=True)
        admin_auth.set_account(conn, "flagged", dev_flags="unlimited,developer")
        admin_auth.register(conn, "carol", "carol-password", trusted_local=True)
        admin_auth.set_account(conn, "carol", dev_flags="nondeveloper")
        return {name: admin_auth.generate_api_key(conn, name)["key"]
                for name in ("dev", "flagged", "carol")}

    keys = _with_db(open_db, mint)
    authorized = []

    def build_http_chat_tool_turn(**kwargs):
        authorized.append(kwargs["authorized_tools"])
        return None

    monkeypatch.setattr(chat_tools_wiring, "build_http_chat_tool_turn", build_http_chat_tool_turn)
    for name, expected in (("dev", True), ("flagged", True), ("carol", False)):
        context = ts._auth_context("Bearer " + keys[name])
        assert context["account"]["username"] == name
        assert ts._developer_authorized(context) is expected, name
        assert (ts._restricted_tool_role(context) is None) is expected, name
        assert ts._admin_authorized(context) is False, name
        status, body = _chat(port, {"Authorization": "Bearer " + keys[name]})
        assert status == 200, (name, body)
        assert authorized and authorized[-1] is expected, name


def test_banning_an_account_revokes_its_keys_for_good(served):
    port, _, open_db, _ = served

    def mint(conn):
        admin_auth.register(conn, "bob", "other-password", trusted_local=True)
        return admin_auth.generate_api_key(conn, "bob")["key"]

    key = _with_db(open_db, mint)
    assert _with_db(open_db, lambda conn: admin_auth.api_key_account(conn, key))["username"] == "bob"
    _with_db(open_db, lambda conn: admin_auth.set_account(conn, "bob", banned=True))
    _with_db(open_db, lambda conn: admin_auth.set_account(conn, "bob", banned=False))
    # Un-banning does not bring back a key issued before the ban.
    assert _with_db(open_db, lambda conn: admin_auth.api_key_account(conn, key)) is None
    assert _with_db(open_db, lambda conn: admin_auth.list_api_keys(conn, "bob")) == []
    assert _chat(port, _both_mode(key))[0] == 401


def test_metering_on_refuses_an_over_budget_key_chat_with_429(served):
    port, _, open_db, _ = served

    def mint(conn):
        admin_auth.register(conn, "bob", "other-password", trusted_local=True)
        return admin_auth.generate_api_key(conn, "bob")["key"]

    key = _with_db(open_db, mint)
    # Metering off (the default): the tier budget is not charged.
    _with_db(open_db, lambda conn: admin_auth.set_saas_tier_limit(conn, "free", 0))
    assert _chat(port, _both_mode(key))[0] == 200
    _with_db(open_db, lambda conn: admin_auth.set_saas_feature(conn, "usage_metering_enabled", True))
    status, body = _chat(port, _both_mode(key))
    assert status == 429, body
    assert body["error"]["type"] == "rate_limit" and "monthly token limit" in body["error"]["message"]


def test_keys_do_not_expire_unless_a_ttl_is_configured(served, monkeypatch):
    port, _, open_db, _ = served
    now = [1_900_000_000]
    monkeypatch.setattr(admin_auth, "_now", lambda: now[0])

    def mint(conn):
        admin_auth.register(conn, "bob", "other-password", trusted_local=True)
        return admin_auth.generate_api_key(conn, "bob")

    forever = _with_db(open_db, mint)
    assert forever["expires_ts"] is None
    _with_db(open_db, lambda conn: admin_auth.set_api_key_ttl(conn, 3600))
    timed = _with_db(open_db, lambda conn: admin_auth.generate_api_key(conn, "bob"))
    assert timed["expires_ts"] == now[0] + 3600
    listed = _with_db(open_db, lambda conn: admin_auth.list_api_keys(conn, "bob"))
    assert sorted((row["key_id"], row["expires_ts"]) for row in listed) == sorted(
        [(forever["key_id"], None), (timed["key_id"], now[0] + 3600)])
    assert _chat(port, _both_mode(timed["key"]))[0] == 200

    now[0] += 3600
    assert _with_db(open_db, lambda conn: admin_auth.api_key_account(conn, timed["key"])) is None
    assert _chat(port, _both_mode(timed["key"]))[0] == 401
    # A key minted with no TTL still works, and clearing the TTL restores the default.
    assert _chat(port, _both_mode(forever["key"]))[0] == 200
    _with_db(open_db, lambda conn: admin_auth.set_api_key_ttl(conn, 0))
    assert _with_db(open_db, lambda conn: admin_auth.generate_api_key(conn, "bob"))["expires_ts"] is None


def _get_json(port, path, headers):
    status, _, raw = _request(port, "GET", path, headers=headers)
    return status, json.loads(raw)


def test_an_admin_key_sees_only_its_own_fanout_runs(served, monkeypatch):
    port, admin_token, _, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    owners = []

    def recent_run_summaries(*, request_owner, include_finished, limit):
        owners.append(request_owner)
        return []

    monkeypatch.setattr(ts.server.fanout_store, "recent_run_summaries", recent_run_summaries)
    # The all-owners operator view needs admin authority, which a key never has.
    status, body = _get_json(port, "/v1/fanout", _both_mode(key))
    assert status == 200, body
    keyed = ts._auth_context("Bearer " + DEPLOYMENT_KEY, key)
    assert owners[-1] == ts._fanout_request_owner(keyed)
    status, body = _get_json(port, "/v1/fanout", _both_mode(admin_token))
    assert status == 200, body
    assert owners[-1] is None

    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    assert _get_json(port, "/v1/fanout", {"Authorization": "Bearer " + key})[0] == 200
    assert owners[-1] == ts._fanout_request_owner(ts._auth_context("Bearer " + key))


def test_an_admin_key_cannot_read_another_owners_fanout_run(served, monkeypatch):
    port, admin_token, _, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    run = {"id": "fr-other", "request_owner": "fo-someone-else"}
    monkeypatch.setattr(ts.server.fanout_store, "get_run",
                        lambda run_id: dict(run) if run_id == run["id"] else None)
    monkeypatch.setattr(ts.server, "_fanout_receipt", lambda run_id: {"id": run_id, "object": "fanout.run"})
    # Another owner's run is not disclosed to an admin account's key ...
    status, body = _get_json(port, "/v1/fanout/fr-other", _both_mode(key))
    assert status == 404, body
    # ... but the admin's signed-in session keeps the recovery path.
    status, body = _get_json(port, "/v1/fanout/fr-other", _both_mode(admin_token))
    assert status == 200 and body["id"] == "fr-other", body


def test_an_admin_accounts_key_is_not_offered_host_bypass_or_admin_features(served):
    port, admin_token, _, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    for path in ("/v1/sonder/whoami", "/v1/sonder/capabilities"):
        status, body = _get_json(port, path, _both_mode(key))
        assert status == 200, (path, body)
        features = body["allowed_features"]
        assert body["host_bypass_allowed"] is False, (path, body)
        assert features["host_bypass"] is False and features["host_admin"] is False, (path, body)
        # The key keeps the developer authority its account's role grants.
        assert features["host_tools"] is True and features["approvals"] is True, (path, body)
        # The same account's signed-in session is the administrator.
        status, body = _get_json(port, path, _both_mode(admin_token))
        assert status == 200, (path, body)
        assert body["host_bypass_allowed"] is True, (path, body)
        assert body["allowed_features"]["host_bypass"] is True, (path, body)
        assert body["allowed_features"]["host_admin"] is True, (path, body)


def test_an_admin_accounts_key_cannot_reach_bypass_or_admin_commands_in_chat(served):
    port, admin_token, _, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    body = json.dumps({"mode": "bypass", "confirm_bypass": True}).encode()
    status, _, raw = _request(port, "POST", "/v1/permission-mode", body=body,
                              headers={"Content-Type": "application/json", **_both_mode(key)})
    assert status == 403, raw
    keyed = ts._auth_context("Bearer " + DEPLOYMENT_KEY, key)
    for line in ("/cloud_opt_in status", "/permission_rule_set file_write allow"):
        out = str(ts._handle_slash(line, context=keyed))
        assert out.startswith("refused /") and "administrator" in out, (line, out)


@pytest.mark.parametrize("bypass_session", ["", "chat-1"], ids=["principal-wide", "same-session"])
def test_an_admin_accounts_key_does_not_inherit_the_bypass_its_session_turned_on(
        served, monkeypatch, bypass_session):
    import permission_modes

    port, admin_token, _, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    seen = []

    def build_http_chat_tool_turn(**kwargs):
        policy = kwargs["permission_policy"]
        seen.append((policy.current_mode(), policy.decide("file_write", interactive=False).action))
        return None

    monkeypatch.setattr(chat_tools_wiring, "build_http_chat_tool_turn", build_http_chat_tool_turn)
    chat = json.dumps({"model": "sonder", "session": "chat-1",
                       "messages": [{"role": "user", "content": "hello"}]}).encode()
    mode = json.dumps({"mode": "bypass", "confirm_bypass": True, "session_id": bypass_session}).encode()
    try:
        status, _, raw = _request(port, "POST", "/v1/permission-mode", body=mode,
                                  headers={"Content-Type": "application/json", **_both_mode(admin_token)})
        assert status == 200 and json.loads(raw)["mode"] == "bypass", raw
        for credential in (admin_token, key):
            status, _, raw = _request(port, "POST", "/v1/chat/completions", body=chat,
                                      headers={"Content-Type": "application/json", **_both_mode(credential)})
            assert status == 200, raw
        # The signed-in session runs its turn in the bypass it turned on ...
        assert seen[0] == ("bypass", "allow"), seen
        # ... but the same account's key does not: a mutation tool is not run unprompted.
        assert seen[1][0] != "bypass" and seen[1][1] in ("ask", "deny"), seen
        keyed = ts._auth_context("Bearer " + DEPLOYMENT_KEY, key)
        with permission_modes.mode_scope(ts._mode_principal(keyed), "chat-1"):
            assert permission_modes.current_mode() != "bypass"
    finally:
        permission_modes._reset_for_tests()


def test_an_admin_accounts_key_carries_developer_not_admin_into_typed_contexts(served):
    from sonder_runtime.adapters.web import lifecycle

    port, admin_token, _, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    keyed = ts._auth_context("Bearer " + DEPLOYMENT_KEY, key)
    assert lifecycle.get().operation_context("corr-key", keyed).auth_level == "developer"
    session = ts._auth_context("Bearer " + DEPLOYMENT_KEY, admin_token)
    assert lifecycle.get().operation_context("corr-session", session).auth_level == "admin"


def test_an_admin_accounts_key_is_metered_and_its_session_is_not(served):
    port, admin_token, open_db, _ = served
    key = _control(port, "api-keys/generate", admin_token)[1]["key"]
    tier = ts._auth_context("Bearer " + DEPLOYMENT_KEY, key)["account"]["tier"]
    _with_db(open_db, lambda conn: admin_auth.set_saas_tier_limit(conn, tier, 0))
    _with_db(open_db, lambda conn: admin_auth.set_saas_feature(conn, "usage_metering_enabled", True))
    # A leaked admin key spends against the tier budget like any other key ...
    status, body = _chat(port, _both_mode(key))
    assert status == 429, body
    assert "monthly token limit" in body["error"]["message"]
    # ... while the admin's signed-in session stays exempt.
    status, body = _chat(port, _both_mode(admin_token))
    assert status == 200, body


class _RacingConnection:
    """A connection whose expires_ts column is added by another process mid-``init``."""

    def __init__(self, conn, rival):
        self._conn, self._rival = conn, rival

    def executescript(self, script):
        return self._conn.executescript(script)

    def execute(self, sql, *args):
        result = self._conn.execute(sql, *args).fetchall()
        if sql.startswith("PRAGMA table_info(account_api_keys)") and self._rival is not None:
            rival, self._rival = self._rival, None
            rival.execute("ALTER TABLE account_api_keys ADD COLUMN expires_ts INTEGER")
            rival.commit()
        return result

    def commit(self):
        return self._conn.commit()


def test_a_concurrent_expiry_upgrade_of_an_old_key_table_does_not_fail(tmp_path):
    import sqlite3

    path = tmp_path / "accounts.db"
    old = sqlite3.connect(path)
    old.execute("CREATE TABLE account_api_keys (key_id TEXT PRIMARY KEY, username TEXT NOT NULL, "
                "key_hash TEXT NOT NULL UNIQUE, created_ts INTEGER NOT NULL)")
    old.commit()
    old.close()
    conn, rival = sqlite3.connect(path), sqlite3.connect(path)
    try:
        # The other process adds the column after this one checked for it.
        admin_auth.init(_RacingConnection(conn, rival))
        columns = [row[1] for row in conn.execute("PRAGMA table_info(account_api_keys)")]
        assert columns.count("expires_ts") == 1
    finally:
        conn.close()
        rival.close()
