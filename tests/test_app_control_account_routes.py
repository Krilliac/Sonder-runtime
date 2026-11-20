"""App-control API-key and swarm-status routes: identity from the session, own keys only."""
import admin_auth
import master_orchestrator

from tests import test_app_control_http as _wire

# The wire fixtures (a started binding over real account and fleet databases).
control = _wire.control
http_control = _wire.http_control
request = _wire.request


def _headers(account_token):
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer deployment-key",
        "X-Sonder-Account-Token": account_token,
    }


def _post(port, route, account_token, body=None):
    return request(port, "POST", "/v1/app-control/" + route, {} if body is None else body,
                   _headers(account_token))


def _user_session(control, username="bob", password="other-password"):
    _, _, _, open_db, _, _ = control
    conn = open_db()
    try:
        account = admin_auth.register(conn, username, password)
        assert account["role"] == "user"
        token, _ = admin_auth.login(conn, username, password)
    finally:
        conn.close()
    return token


def test_a_raw_header_value_is_not_an_identity_for_any_account_route(http_control):
    port, _, _ = http_control
    for route in ("api-keys/list", "api-keys/generate", "api-keys/revoke", "swarm/status"):
        for forged in ("root", "admin", "alice", "x"):
            status, headers, body = _post(port, route, forged)
            assert status == 401, (route, forged, body)
            assert body["error"]["code"] == "APP_CONTROL_AUTH_REQUIRED"
            assert headers["Cache-Control"] == "no-store"


def test_swarm_status_needs_an_admin_session(http_control, control, monkeypatch):
    port, admin_token, _ = http_control
    monkeypatch.setitem(master_orchestrator._AGENTS, "agent-1",
                        {"id": "agent-1", "owner_id": "someone", "status": "running"})
    status, _, body = _post(port, "swarm/status", admin_token)
    assert status == 200 and body["ok"] is True
    assert [row["id"] for row in body["status"]] == ["agent-1"]
    status, _, body = _post(port, "swarm/status", _user_session(control))
    assert status == 401 and "status" not in body


def test_api_keys_are_generated_listed_and_revoked_for_the_session_account(http_control, control):
    port, admin_token, _ = http_control
    status, _, body = _post(port, "api-keys/generate", admin_token)
    assert status == 200 and body["ok"] is True
    key, key_id = body["key"], body["key_id"]
    assert key.startswith("sk-") and key_id
    status, _, listed = _post(port, "api-keys/list", admin_token)
    assert status == 200 and [row["key_id"] for row in listed["keys"]] == [key_id]
    # The secret and its hash are never listed.
    assert key not in str(listed) and "key_hash" not in str(listed)
    _, _, _, open_db, _, _ = control
    conn = open_db()
    try:
        assert admin_auth.authenticate_api_key(conn, key) == "alice"
    finally:
        conn.close()
    status, _, body = _post(port, "api-keys/revoke", admin_token, {"key_id": key_id})
    assert status == 200 and body == {"ok": True, "revoked": 1}
    assert _post(port, "api-keys/list", admin_token)[2]["keys"] == []
    conn = open_db()
    try:
        assert admin_auth.authenticate_api_key(conn, key) is None
    finally:
        conn.close()


def test_an_ordinary_account_manages_only_its_own_keys(http_control, control):
    port, admin_token, _ = http_control
    bob = _user_session(control)
    alice_key_id = _post(port, "api-keys/generate", admin_token)[2]["key_id"]
    status, _, body = _post(port, "api-keys/generate", bob)
    assert status == 200
    bob_key_id = body["key_id"]
    assert [row["key_id"] for row in _post(port, "api-keys/list", bob)[2]["keys"]] == [bob_key_id]
    # Bob cannot revoke Alice's key by naming it, and the attempt changes nothing.
    status, _, body = _post(port, "api-keys/revoke", bob, {"key_id": alice_key_id})
    assert status == 404
    assert [row["key_id"] for row in _post(port, "api-keys/list", admin_token)[2]["keys"]] == [alice_key_id]
    # An empty body revokes every key of the caller, and only the caller's.
    status, _, body = _post(port, "api-keys/revoke", bob)
    assert status == 200 and body["revoked"] == 1
    assert _post(port, "api-keys/list", bob)[2]["keys"] == []
    assert len(_post(port, "api-keys/list", admin_token)[2]["keys"]) == 1


def test_account_routes_refuse_unexpected_body_fields(http_control):
    port, admin_token, _ = http_control
    assert _post(port, "api-keys/generate", admin_token, {"username": "bob"})[0] == 400
    assert _post(port, "api-keys/list", admin_token, {"username": "bob"})[0] == 400
    assert _post(port, "api-keys/revoke", admin_token, {"username": "bob"})[0] == 400
    assert _post(port, "swarm/status", admin_token, {"owner": "bob"})[0] == 400


def test_account_routes_still_need_the_deployment_key(http_control):
    port, admin_token, _ = http_control
    headers = _headers(admin_token)
    del headers["Authorization"]
    status, _, body = request(port, "POST", "/v1/app-control/api-keys/list", {}, headers)
    assert status == 401
