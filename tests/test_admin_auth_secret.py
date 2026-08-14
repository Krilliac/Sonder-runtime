"""C3 regression: the account-session HMAC key must never be the public dev
constant.  When SONDER_AUTH_SECRET is unset a per-install random secret is
derived and persisted 0600; account-bearing config modes refuse the built-in
default; the loopback no-account profile keeps working with zero configuration.
"""
import hashlib
import hmac
import os
import stat

import pytest

import admin_auth
import memory_store
import sonder_config
import sonder_secrets


@pytest.fixture
def isolated_home(monkeypatch, tmp_path):
    """Point per-install state at a temp home with no explicit auth secret."""
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    monkeypatch.delenv("SONDER_AUTH_SECRET", raising=False)
    return tmp_path


def test_secret_generated_and_persisted_when_unset(isolated_home):
    secret = admin_auth._secret()

    path = admin_auth._auth_secret_file()
    assert path.exists()
    assert path.read_text(encoding="utf-8").strip() == secret
    assert secret
    assert secret != admin_auth.PUBLIC_DEV_SECRET
    # A freshly derived secret is at least as strong as the non-loopback rule
    # (>=32 chars) demands, so it never trips the weak-key gate.
    assert len(secret) >= 32


@pytest.mark.skipif(os.name != "posix", reason="POSIX file modes only")
def test_persisted_secret_is_private_0600(isolated_home):
    admin_auth._secret()

    mode = stat.S_IMODE(admin_auth._auth_secret_file().stat().st_mode)
    assert mode == 0o600


def test_secret_is_stable_across_calls(isolated_home):
    first = admin_auth._secret()
    # A second call must return the persisted value, not mint a new one.
    second = admin_auth._secret()

    assert first == second


def test_private_create_never_replaces_a_first_run_secret(tmp_path):
    """Concurrent startup must have one durable winner, including Windows."""
    path = tmp_path / "secrets" / "auth_secret"

    assert sonder_secrets._create_private_if_missing(path, "first") is True
    assert sonder_secrets._create_private_if_missing(path, "second") is False
    assert path.read_text(encoding="utf-8") == "first"


def test_secret_uses_the_other_process_first_run_value(monkeypatch, isolated_home):
    """A losing initializer re-reads the atomic winner instead of its draft."""
    path = admin_auth._auth_secret_file()
    winner = "winner-" + "w" * 40

    def lose_create(created_path, content):
        assert created_path == path
        assert content != winner
        sonder_secrets._write_private(path, winner)
        return False

    monkeypatch.setattr(sonder_secrets, "_create_private_if_missing", lose_create)
    assert admin_auth._secret() == winner


def test_secret_never_uses_an_unpersisted_first_run_value(monkeypatch, isolated_home):
    monkeypatch.setattr(
        sonder_secrets, "_create_private_if_missing", lambda path, content: False
    )

    with pytest.raises(RuntimeError, match="persisted account-session secret"):
        admin_auth._secret()


def test_public_constant_is_never_used_to_hash_a_real_token(isolated_home):
    token = "a" * 48
    persisted = admin_auth._secret()

    public_hash = hmac.new(
        admin_auth.PUBLIC_DEV_SECRET.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    persisted_hash = hmac.new(
        persisted.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()

    assert admin_auth._hash_token(token) == persisted_hash
    assert admin_auth._hash_token(token) != public_hash


def test_generated_secret_is_random_per_install(monkeypatch, tmp_path):
    monkeypatch.delenv("SONDER_AUTH_SECRET", raising=False)

    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "a"))
    secret_a = admin_auth._secret()
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "b"))
    secret_b = admin_auth._secret()

    assert secret_a != secret_b


def test_explicit_env_secret_is_the_escape_hatch(monkeypatch, tmp_path):
    """An operator-provided secret always wins and nothing is persisted."""
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    monkeypatch.setenv("SONDER_AUTH_SECRET", "operator-provided-" + "z" * 32)

    assert admin_auth._secret() == "operator-provided-" + "z" * 32
    assert not admin_auth._auth_secret_file().exists()


def test_full_login_round_trip_uses_the_derived_secret(isolated_home):
    conn = memory_store.connect(":memory:")
    admin_auth.register(conn, "owner", "password123")

    token, account = admin_auth.login(conn, "owner", "password123")
    assert account["username"] == "owner"

    # The session row is keyed by the derived-secret HMAC, so authentication
    # only succeeds because the persisted (not the public) secret was used.
    assert admin_auth.authenticate(conn, token)["username"] == "owner"
    forged_hash = hmac.new(
        admin_auth.PUBLIC_DEV_SECRET.encode("utf-8"),
        token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    row = conn.execute(
        "SELECT token_hash FROM account_sessions WHERE username=?", ("owner",)
    ).fetchone()
    assert row["token_hash"] != forged_hash


def test_loopback_no_account_profile_needs_no_secret_file(isolated_home):
    """The single-user local profile does no token hashing, so zero config
    stays zero config: no accounts, no forced secret materialisation."""
    conn = memory_store.connect(":memory:")

    assert admin_auth.account_count(conn) == 0
    # An empty/missing token short-circuits before any HMAC is computed.
    assert admin_auth.authenticate(conn, "") is None
    assert not admin_auth._auth_secret_file().exists()


def _base_env(tmp_path, **extra):
    env = {"SONDER_HOME": str(tmp_path)}
    env.update(extra)
    return env


def test_config_refuses_account_mode_with_default_secret(tmp_path):
    for mode in sonder_config.ACCOUNT_BEARING_AUTH_MODES:
        env = _base_env(
            tmp_path,
            SONDER_AUTH_MODE=mode,
            SONDER_AUTH_SECRET=sonder_config.BUILTIN_DEV_AUTH_SECRET,
        )
        with pytest.raises(sonder_config.ConfigError) as excinfo:
            sonder_config.load_config(env=env)
        assert any(
            "built-in development auth secret" in err
            for err in excinfo.value.errors
        ), mode


def test_config_allows_account_mode_with_strong_secret(tmp_path):
    env = _base_env(
        tmp_path,
        SONDER_AUTH_MODE="account",
        SONDER_AUTH_SECRET="s" * 40,
    )
    config = sonder_config.load_config(env=env)
    assert config.server.auth_mode == "account"


def test_config_allows_account_mode_with_no_explicit_secret(tmp_path):
    """Empty auth_secret is fine now that admin_auth derives one at runtime;
    only the public default constant is refused for account-bearing modes."""
    env = _base_env(tmp_path, SONDER_AUTH_MODE="account")
    config = sonder_config.load_config(env=env)
    assert config.server.auth_mode == "account"
    assert config.secrets.auth_secret == ""


def test_config_default_loopback_profile_unaffected(tmp_path):
    config = sonder_config.load_config(env=_base_env(tmp_path))
    assert config.server.auth_mode == "api-key"
    assert config.secrets.auth_secret == ""
