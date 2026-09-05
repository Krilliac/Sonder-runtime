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


def test_session_reauthentication_checks_password_without_minting_login_token():
    conn = memory_store.connect(':memory:')
    try:
        admin_auth.register(conn, 'owner', 'password123')
        token, expected = admin_auth.login(conn, 'owner', 'password123')
        before = [tuple(row) for row in conn.execute('SELECT * FROM account_sessions')]
        account = admin_auth.reauthenticate(conn, token, 'password123')
        assert account == expected
        assert not any('password' in key or 'token' in key for key in account)
        assert [tuple(row) for row in conn.execute('SELECT * FROM account_sessions')] == before
        with pytest.raises(PermissionError):
            admin_auth.reauthenticate(conn, token, 'wrong-password')
        assert admin_auth.authenticate(conn, token) is not None
    finally:
        conn.close()


def test_session_reauthentication_refuses_revoked_or_expired_session():
    conn = memory_store.connect(':memory:')
    try:
        admin_auth.register(conn, 'owner', 'password123')
        token, _ = admin_auth.login(conn, 'owner', 'password123')
        conn.execute('UPDATE account_sessions SET expires_ts=?', (admin_auth._now(),))
        conn.commit()
        with pytest.raises(PermissionError):
            admin_auth.reauthenticate(conn, token, 'password123')
        token, _ = admin_auth.login(conn, 'owner', 'password123')
        admin_auth.set_account(conn, 'owner', banned=True)
        admin_auth.set_account(conn, 'owner', banned=False)
        with pytest.raises(PermissionError):
            admin_auth.reauthenticate(conn, token, 'password123')
    finally:
        conn.close()


def test_session_reauthentication_preserves_caller_transaction():
    conn = memory_store.connect(':memory:')
    try:
        admin_auth.register(conn, 'owner', 'password123')
        token, _ = admin_auth.login(conn, 'owner', 'password123')
        conn.execute("UPDATE accounts SET tier='pro' WHERE username='owner'")
        with pytest.raises(PermissionError, match='idle owned connection'):
            admin_auth.reauthenticate(conn, token, 'password123')
        assert conn.in_transaction
        conn.rollback()
        assert admin_auth.public_account(conn, 'owner')['tier'] == 'free'
    finally:
        conn.close()


def test_session_reauthentication_returns_live_role_and_checks_expiry_after_hash(monkeypatch):
    conn = memory_store.connect(':memory:')
    try:
        admin_auth.register(conn, 'owner', 'password123')
        token, _ = admin_auth.login(conn, 'owner', 'password123')
        admin_auth.set_account(conn, 'owner', role='user')
        assert admin_auth.reauthenticate(conn, token, 'password123')['role'] == 'user'
        now = admin_auth._now()
        conn.execute('UPDATE account_sessions SET expires_ts=?', (now + 1,))
        conn.commit()
        clock = [now]
        hash_password = admin_auth._hash_password
        def delayed_hash(*args):
            result = hash_password(*args)
            clock[0] += 2
            return result
        monkeypatch.setattr(admin_auth, '_now', lambda: clock[0])
        monkeypatch.setattr(admin_auth, '_hash_password', delayed_hash)
        with pytest.raises(PermissionError):
            admin_auth.reauthenticate(conn, token, 'password123')
        assert not conn.in_transaction
    finally:
        conn.close()


def test_banned_account_cannot_login_or_authenticate():
    conn = memory_store.connect(":memory:")
    admin_auth.register(conn, "user1", "password123")
    token, _ = admin_auth.login(conn, "user1", "password123")
    admin_auth.set_account(conn, "user1", banned=True)

    assert admin_auth.authenticate(conn, token) is None
    with pytest.raises(PermissionError):
        admin_auth.login(conn, "user1", "password123")


def test_ban_revokes_existing_sessions_even_after_unban():
    conn = memory_store.connect(":memory:")
    admin_auth.register(conn, "user1", "password123")
    token, _ = admin_auth.login(conn, "user1", "password123")

    admin_auth.set_account(conn, "user1", banned=True)
    admin_auth.set_account(conn, "user1", banned=False)

    # Unbanning permits a new login, but it must never revive a bearer token
    # issued before the administrative ban.
    assert admin_auth.authenticate(conn, token) is None
    fresh_token, account = admin_auth.login(conn, "user1", "password123")
    assert fresh_token != token
    assert account["username"] == "user1"
    assert admin_auth.authenticate(conn, fresh_token)["username"] == "user1"


def test_rate_limit_blocks_free_tier_after_limit():
    conn = memory_store.connect(":memory:")
    account = admin_auth.register(conn, "user1", "password123")

    ok, msg = admin_auth.rate_limit(conn, account, cost=admin_auth.DEFAULT_RATE_LIMIT + 1)

    assert ok is False
    assert "rate limit" in msg


def test_rate_limit_is_atomic_across_concurrent_connections(monkeypatch, tmp_path):
    """Concurrent HTTP connections cannot each admit the same final slot."""
    path = str(tmp_path / "accounts.db")
    initial = memory_store.connect(path)
    account = admin_auth.register(initial, "user1", "password123")
    initial.close()
    monkeypatch.setattr(admin_auth, "_now", lambda: 1_700_000_000)

    attempts = admin_auth.DEFAULT_RATE_LIMIT + 12
    barrier = threading.Barrier(attempts)
    outcomes = []
    outcome_lock = threading.Lock()

    def attempt():
        conn = memory_store.connect(path)
        try:
            barrier.wait()
            outcome = admin_auth.rate_limit(conn, account)
            with outcome_lock:
                outcomes.append(outcome)
        finally:
            conn.close()

    workers = [threading.Thread(target=attempt) for _ in range(attempts)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=30)

    assert len(outcomes) == attempts
    assert sum(ok for ok, _message in outcomes) == admin_auth.DEFAULT_RATE_LIMIT
    check = memory_store.connect(path)
    try:
        row = check.execute(
            "SELECT count FROM account_rate WHERE username=?",
            (account["username"],),
        ).fetchone()
        assert row["count"] == attempts
    finally:
        check.close()


def test_public_bootstrap_requires_secret_and_is_one_use(monkeypatch):
    secret = "bootstrap-secret-123456"
    monkeypatch.setenv("SONDER_BOOTSTRAP_SECRET", secret)
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
    monkeypatch.setenv("SONDER_BOOTSTRAP_SECRET", secret)
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


@pytest.mark.parametrize("username", ["ab", "u" * 129, "bad\x00name", 123])
def test_registration_rejects_unbounded_or_nonprintable_usernames(username):
    conn = memory_store.connect(":memory:")
    with pytest.raises(ValueError, match="username"):
        admin_auth.register(conn, username, "password123")


def test_registration_and_login_bound_password_work():
    conn = memory_store.connect(":memory:")
    admin_auth.register(conn, "bounded-user", "p" * admin_auth.MAX_PASSWORD_CHARS)
    token, account = admin_auth.login(
        conn, "bounded-user", "p" * admin_auth.MAX_PASSWORD_CHARS,
    )
    assert token and account["username"] == "bounded-user"
    with pytest.raises(ValueError, match="invalid username or password"):
        admin_auth.login(
            conn, "bounded-user", "p" * (admin_auth.MAX_PASSWORD_CHARS + 1),
        )
