import threading

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


def test_public_bootstrap_requires_secret_and_is_one_use(monkeypatch):
    secret = "bootstrap-secret-123456"
    monkeypatch.setenv("TRILOBITE_BOOTSTRAP_SECRET", secret)
    conn = memory_store.connect(":memory:")

    with pytest.raises(PermissionError):
        admin_auth.register(
            conn, "owner", "password123", trusted_local=False
        )
    account = admin_auth.register(
        conn,
        "owner",
        "password123",
        trusted_local=False,
        bootstrap_secret=secret,
    )
    assert account["role"] == "admin"
    with pytest.raises(PermissionError):
        admin_auth.register(
            conn,
            "other",
            "password123",
            trusted_local=False,
            bootstrap_secret=secret,
        )


def test_concurrent_bootstrap_creates_exactly_one_admin(monkeypatch, tmp_path):
    secret = "bootstrap-secret-123456"
    monkeypatch.setenv("TRILOBITE_BOOTSTRAP_SECRET", secret)
    path = str(tmp_path / "concurrent.db")
    initial = memory_store.connect(path)
    admin_auth.init(initial)
    initial.close()
    barrier = threading.Barrier(2)
    results = []

    def bootstrap(username):
        conn = memory_store.connect(path)
        barrier.wait()
        try:
            account = admin_auth.register(
                conn,
                username,
                "password123",
                trusted_local=False,
                bootstrap_secret=secret,
            )
            results.append(("ok", account["role"]))
        except PermissionError:
            results.append(("denied", None))
        finally:
            conn.close()

    threads = [
        threading.Thread(target=bootstrap, args=("owner1",)),
        threading.Thread(target=bootstrap, args=("owner2",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert sorted(result[0] for result in results) == ["denied", "ok"]
    check = memory_store.connect(path)
    assert admin_auth.account_count(check) == 1
    assert admin_auth.list_accounts(check)[0]["role"] == "admin"
    check.close()

