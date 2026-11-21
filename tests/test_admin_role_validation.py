"""An account role may only be one of the known roles.

``admin_auth.set_account`` used to store any ``role`` value it was handed, so
an admin typo (``role=Admin``) or a client bug wrote a role no gate recognises.
Every entry point reaches that one function: the HTTP route
``POST /v1/sonder/admin/account``, the ``admin_set_account`` MCP tool, and the
``/setaccount`` command of both consoles. Each is exercised here against a
real account store, and each asserts the stored row is byte-for-byte the same
after the refused call.
"""
from __future__ import annotations

import http.client
import json
import threading

import pytest

import admin_auth
import memory_store
import server
import sonder_runtime.interfaces.http.serve as ts
import sonder_runtime.interfaces.repl.repl as sonder_repl

UNKNOWN_ROLES = ["root", "superadmin", "Admin", "ADMIN", " admin", "admin ", "owner", "user;admin"]
NON_STRING_ROLES = ["", None, 1, True, b"admin", ["admin"]]


def _snapshot(path):
    """Everything a refused call must leave alone: the rows, not a public view."""
    conn = memory_store.connect(path)
    try:
        accounts = [tuple(row) for row in conn.execute(
            "SELECT username, role, tier, dev_flags, banned FROM accounts ORDER BY username"
        )]
        sessions = [tuple(row) for row in conn.execute(
            "SELECT username, revoked FROM account_sessions ORDER BY username, token_hash"
        )]
    finally:
        conn.close()
    return accounts, sessions


@pytest.fixture
def store(tmp_path, monkeypatch, _configure_http_legacy_boundary):
    """A real store with owner (admin) and usr1 (user), wired into ``server``."""
    monkeypatch.setenv("SONDER_AUTH_SECRET", "role-validation-test-secret")
    path = str(tmp_path / "accounts.db")
    conn = memory_store.connect(path)
    try:
        admin_auth.register(conn, "owner", "password123")
        admin_auth.register(conn, "usr1", "password123")
        admin_token = admin_auth.login(conn, "owner", "password123")[0]
        admin_auth.login(conn, "usr1", "password123")
    finally:
        conn.close()
    monkeypatch.setattr(server, "_open_db", lambda: memory_store.connect(path))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *_a, **_k: None)
    before = _snapshot(path)
    # Guard the guard: the fixture really holds a user row to (not) change.
    assert ("usr1", "user", "free", "", 0) in [
        (name, role, tier, flags or "", banned) for name, role, tier, flags, banned in before[0]
    ]
    assert len(before[1]) == 2
    return path, admin_token, before


# --- the store function ------------------------------------------------------------


@pytest.mark.parametrize("role", UNKNOWN_ROLES + NON_STRING_ROLES, ids=repr)
def test_set_account_refuses_an_unknown_role_and_changes_nothing(store, role):
    path, _token, before = store
    conn = memory_store.connect(path)
    try:
        with pytest.raises(ValueError, match="role must be one of: user, developer, admin"):
            admin_auth.set_account(conn, "usr1", role=role)
        assert not conn.in_transaction
    finally:
        conn.close()
    assert _snapshot(path) == before


def test_set_account_applies_no_part_of_a_call_with_an_unknown_role(store):
    """A bad role must not let the valid changes beside it through."""
    path, _token, before = store
    conn = memory_store.connect(path)
    try:
        with pytest.raises(ValueError, match="role must be one of"):
            admin_auth.set_account(
                conn, "usr1", role="root", tier="enterprise", dev_flags="unlimited", banned=True,
            )
    finally:
        conn.close()
    # Tier, flags and ban are unchanged, and the ban did not revoke the session.
    assert _snapshot(path) == before


@pytest.mark.parametrize("role", admin_auth.ACCOUNT_ROLES)
def test_set_account_still_stores_every_known_role(store, role):
    path, _token, _before = store
    conn = memory_store.connect(path)
    try:
        assert admin_auth.set_account(conn, "usr1", role=role)["role"] == role
    finally:
        conn.close()
    assert ("usr1", role) in [(row[0], row[1]) for row in _snapshot(path)[0]]


def test_known_roles_are_exactly_the_ones_the_gates_rank():
    assert admin_auth.ACCOUNT_ROLES == ("user", "developer", "admin")
    for role in admin_auth.ACCOUNT_ROLES:
        assert admin_auth.require({"role": "admin"}, role) == (True, "")


def test_set_account_without_a_role_is_unaffected(store):
    path, _token, _before = store
    conn = memory_store.connect(path)
    try:
        account = admin_auth.set_account(conn, "usr1", tier="pro")
    finally:
        conn.close()
    assert (account["role"], account["tier"]) == ("user", "pro")


# --- MCP tool ----------------------------------------------------------------------


@pytest.mark.parametrize("role", UNKNOWN_ROLES, ids=repr)
def test_mcp_tool_refuses_an_unknown_role_and_changes_nothing(store, role):
    path, token, before = store

    out = server.admin_set_account(token=token, username="usr1", role=role)

    assert out == "ERROR: role must be one of: user, developer, admin"
    assert _snapshot(path) == before


def test_mcp_tool_refuses_the_whole_call_when_the_role_is_unknown(store):
    path, token, before = store

    out = server.admin_set_account(
        token=token, username="usr1", role="root", tier="enterprise", banned="true",
    )

    assert out.startswith("ERROR: role must be one of")
    assert _snapshot(path) == before


def test_mcp_tool_still_sets_a_known_role(store):
    path, token, _before = store

    out = server.admin_set_account(token=token, username="usr1", role="developer")

    assert out.startswith("updated ")
    assert ("usr1", "developer") in [(row[0], row[1]) for row in _snapshot(path)[0]]


# --- HTTP route --------------------------------------------------------------------


def _post_admin_account(monkeypatch, token, payload):
    admin = {
        "mode": "account", "authorized": True, "api_key": False,
        "account": {"username": "owner", "role": "admin"},
    }
    monkeypatch.setattr(ts.Handler, "_request_auth_context", lambda _self: admin)
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    # What serve.run() does before it binds, so the request does not pay a cold
    # import inside the handler and outlast the client timeout.
    ts._live_telemetry_application(build=True)
    warm_operations = getattr(ts.sonder_lifecycle.get(), "operations", None)
    if callable(warm_operations):
        warm_operations()
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        conn = http.client.HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=30)
        conn.request(
            "POST", "/v1/sonder/admin/account", body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json", "X-Sonder-Account-Token": token},
        )
        response = conn.getresponse()
        result = response.status, json.loads(response.read())
        conn.close()
        return result
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("role", ["root", "Admin", 7, ["admin"]], ids=repr)
def test_http_route_refuses_an_unknown_role_and_changes_nothing(store, monkeypatch, role):
    path, token, before = store

    status, body = _post_admin_account(
        monkeypatch, token, {"username": "usr1", "role": role, "tier": "enterprise"},
    )

    assert status == 400
    assert body == {"error": {
        "message": "role must be one of: user, developer, admin", "type": "invalid_request",
    }}
    assert _snapshot(path) == before


def test_http_route_still_sets_a_known_role(store, monkeypatch):
    path, token, _before = store

    status, body = _post_admin_account(monkeypatch, token, {"username": "usr1", "role": "developer"})

    assert status == 200 and body["ok"] is True
    assert ("usr1", "developer") in [(row[0], row[1]) for row in _snapshot(path)[0]]


# --- consoles (/setaccount) --------------------------------------------------------


def test_served_console_setaccount_refuses_an_unknown_role(store, monkeypatch):
    path, token, before = store
    monkeypatch.setattr(ts, "_http_slash_refusal", lambda *_a, **_k: "")
    state = ts.ConversationState()
    state.token = token

    out = ts._handle_slash("/setaccount usr1 role=root tier=enterprise banned=true", state=state)

    assert out.startswith("ERROR: role must be one of")
    assert _snapshot(path) == before


def test_repl_setaccount_refuses_an_unknown_role(store, monkeypatch, capsys):
    path, token, before = store
    lines = iter(("/setaccount usr1 role=root tier=enterprise banned=true", "/exit"))
    monkeypatch.setattr(sonder_repl, "_read_input", lambda *_a, **_k: next(lines))
    monkeypatch.setattr(sonder_repl, "_startup_banner", lambda *_args: "")
    monkeypatch.setattr(sonder_repl, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(sonder_repl, "_named_command_gate", lambda _cmd, _argument="": (True, ""))
    monkeypatch.setattr(sonder_repl, "_latest_repl_turn_metrics", lambda *_a, **_k: None)
    monkeypatch.setattr(sonder_repl, "CURRENT_TOKEN", token)

    sonder_repl.main()

    assert "ERROR: role must be one of: user, developer, admin" in capsys.readouterr().out
    assert _snapshot(path) == before
