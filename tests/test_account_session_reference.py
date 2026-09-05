"""Private exact-login references support live background admission checks."""

import pytest
import sqlite3

import admin_auth
import memory_store
from sonder_runtime.adapters.security.account_auth import account_auth


@pytest.fixture
def login(tmp_path):
    path = str(tmp_path / "accounts.sqlite")
    conn = memory_store.connect(path)
    admin_auth.register(conn, "owner", "password123")
    first, _ = admin_auth.login(conn, "owner", "password123")
    second, _ = admin_auth.login(conn, "owner", "password123")
    try:
        yield conn, path, first, second
    finally:
        conn.close()


def test_exact_login_reference_survives_reopen_and_never_authenticates_as_bearer(login):
    conn, path, first, second = login
    before = list(conn.iterdump())
    identity = account_auth.authenticate_session(conn, first)
    other = account_auth.authenticate_session(conn, second)
    assert identity.username == "owner" and identity.role == "admin"
    assert identity.reference != other.reference
    assert identity.reference not in repr(identity) and first not in repr(identity)
    assert list(conn.iterdump()) == before
    assert admin_auth.authenticate(conn, identity.reference) is None
    reopened = memory_store.connect(path)
    try:
        assert (
            account_auth.read_session_reference(reopened, identity.reference)
            == identity
        )
    finally:
        reopened.close()
    admin_auth.revoke_session(conn, first)
    assert account_auth.read_session_reference(conn, identity.reference) is None
    assert account_auth.read_session_reference(conn, other.reference) == other


def test_reference_checks_current_role_ban_and_exact_expiry(login, monkeypatch):
    conn, _, first, _ = login
    identity = account_auth.authenticate_session(conn, first)
    admin_auth.set_account(conn, "owner", role="user")
    assert account_auth.read_session_reference(conn, identity.reference).role == "user"
    monkeypatch.setattr(admin_auth, "_now", lambda: identity.expires_at)
    assert account_auth.read_session_reference(conn, identity.reference) is None
    monkeypatch.setattr(admin_auth, "_now", lambda: identity.expires_at - 1)
    assert account_auth.read_session_reference(conn, identity.reference) is not None
    admin_auth.set_account(conn, "owner", banned=True)
    admin_auth.set_account(conn, "owner", banned=False)
    assert account_auth.read_session_reference(conn, identity.reference) is None


@pytest.mark.parametrize("method", ["authenticate_session", "read_session_reference"])
def test_session_lookup_preserves_caller_transaction(login, method):
    conn, _, first, _ = login
    reference = account_auth.authenticate_session(conn, first).reference
    conn.execute("UPDATE accounts SET tier='pro' WHERE username='owner'")
    with pytest.raises(PermissionError, match="idle owned connection"):
        getattr(account_auth, method)(
            conn, first if method == "authenticate_session" else reference
        )
    assert conn.in_transaction
    conn.rollback()
    assert admin_auth.public_account(conn, "owner")["tier"] == "free"


@pytest.mark.parametrize("value", [None, "", 5, "x" * 513, "\ud800", "\n", "unknown"])
def test_invalid_credentials_and_references_are_inert(login, value):
    conn, _, _, _ = login
    before = list(conn.iterdump())
    assert account_auth.authenticate_session(conn, value) is None
    assert account_auth.read_session_reference(conn, value) is None
    assert list(conn.iterdump()) == before


def test_missing_schema_is_unavailable_and_lookup_does_not_initialize_it():
    conn = memory_store.connect(":memory:")
    try:
        with pytest.raises(sqlite3.OperationalError):
            account_auth.read_session_reference(conn, "account-session-v1:" + "0" * 64)
        assert not conn.in_transaction
        assert (
            conn.execute(
                "SELECT name FROM sqlite_master WHERE name='accounts'"
            ).fetchone()
            is None
        )
    finally:
        conn.close()


def test_reference_sees_committed_revocation_from_another_connection(login):
    conn, path, first, _ = login
    identity = account_auth.authenticate_session(conn, first)
    writer = memory_store.connect(path)
    try:
        admin_auth.revoke_session(writer, first)
    finally:
        writer.close()
    assert account_auth.read_session_reference(conn, identity.reference) is None
    assert not conn.in_transaction
