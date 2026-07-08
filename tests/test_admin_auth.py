import pytest

import admin_auth
import memory_store


def test_first_registered_account_becomes_admin():
    conn = memory_store.connect(":memory:")
    account = admin_auth.register(conn, "Owner", "password123")

    assert account["username"] == "owner"
    assert account["role"] == "admin"


def test_login_returns_authenticatable_token():
    conn = memory_store.connect(":memory:")
    admin_auth.register(conn, "user1", "password123")

    token, account = admin_auth.login(conn, "user1", "password123")

    assert token
    assert account["username"] == "user1"
    assert admin_auth.authenticate(conn, token)["username"] == "user1"


def test_banned_account_cannot_login_or_authenticate():
    conn = memory_store.connect(":memory:")
    admin_auth.register(conn, "user1", "password123")
    token, _ = admin_auth.login(conn, "user1", "password123")
    admin_auth.set_account(conn, "user1", banned=True)

    assert admin_auth.authenticate(conn, token) is None
    with pytest.raises(PermissionError):
        admin_auth.login(conn, "user1", "password123")


def test_rate_limit_blocks_free_tier_after_limit():
    conn = memory_store.connect(":memory:")
    account = admin_auth.register(conn, "user1", "password123")

    ok, msg = admin_auth.rate_limit(conn, account, cost=admin_auth.DEFAULT_RATE_LIMIT + 1)

    assert ok is False
    assert "rate limit" in msg

