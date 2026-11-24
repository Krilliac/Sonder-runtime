"""SaaS switches and monthly token metering: OFF by default, never for session admins or 'unlimited'."""
import ast
import threading
from pathlib import Path

import pytest

import admin_auth
import memory_store
from sonder_runtime.application.config.saas_config import FEATURES, SaaSConfig

ROOT = Path(__file__).resolve().parents[1]
TOKENS = admin_auth.CHAT_REQUEST_TOKEN_ESTIMATE


@pytest.fixture
def conn():
    connection = memory_store.connect(":memory:")
    admin_auth.register(connection, "owner", "password123")  # first account: admin
    admin_auth.register(connection, "alice", "password123")  # role user, tier free
    yield connection
    connection.close()


def _free_limit_in_requests(connection):
    return admin_auth.saas_config(connection).get_tier_limit("free") // TOKENS


def test_every_saas_feature_is_off_by_default(conn):
    config = admin_auth.saas_config(conn)
    assert FEATURES and all(config.is_feature_enabled(name) is False for name in FEATURES)
    assert SaaSConfig.from_rows(()).is_feature_enabled("usage_metering_enabled") is False
    assert config.is_feature_enabled("unknown_feature") is False


def test_unknown_tier_falls_back_to_free_limit(conn):
    config = admin_auth.saas_config(conn)
    assert config.get_tier_limit("mystery-tier") == config.get_tier_limit("free")
    assert config.get_tier_limit("") == config.get_tier_limit("free")


def test_metering_off_never_caps_a_free_account(conn):
    alice = admin_auth.public_account(conn, "alice")
    for _ in range(_free_limit_in_requests(conn) + 5):
        assert admin_auth.meter_usage(conn, alice, TOKENS) == (True, "")
    assert conn.execute("SELECT COUNT(*) FROM usage_stats").fetchone()[0] == 0


def test_metering_on_caps_a_free_account_at_its_tier_limit(conn):
    admin_auth.set_saas_feature(conn, "usage_metering_enabled", True)
    alice = admin_auth.public_account(conn, "alice")
    allowed = _free_limit_in_requests(conn)
    for _ in range(allowed):
        assert admin_auth.meter_usage(conn, alice, TOKENS)[0] is True
    ok, message = admin_auth.meter_usage(conn, alice, TOKENS)
    assert ok is False and "monthly token limit" in message and "free" in message
    # A refused request is not charged.
    row = conn.execute("SELECT monthly_count FROM usage_stats WHERE username='alice'").fetchone()
    assert row[0] == allowed * TOKENS


def test_metering_on_never_meters_session_admins_unlimited_or_callers_without_an_account(conn):
    admin_auth.set_saas_feature(conn, "usage_metering_enabled", True)
    admin_auth.set_saas_tier_limit(conn, "free", TOKENS)
    owner = admin_auth.public_account(conn, "owner")
    assert owner["role"] == "admin"
    conn.execute("UPDATE accounts SET dev_flags='unlimited' WHERE username='alice'")
    conn.commit()
    alice = admin_auth.public_account(conn, "alice")
    for _ in range(5):
        assert admin_auth.meter_usage(conn, owner, TOKENS) == (True, "")
        assert admin_auth.meter_usage(conn, alice, TOKENS) == (True, "")
        assert admin_auth.meter_usage(conn, None, TOKENS) == (True, "")
    assert conn.execute("SELECT COUNT(*) FROM usage_stats").fetchone()[0] == 0


def test_metering_on_meters_an_admin_authenticating_via_api_key(conn):
    admin_auth.set_saas_feature(conn, "usage_metering_enabled", True)
    admin_auth.set_saas_tier_limit(conn, "free", TOKENS)
    minted = admin_auth.generate_api_key(conn, "owner")
    owner_via_key = admin_auth.api_key_account(conn, minted["key"])
    assert owner_via_key is not None and owner_via_key["via_api_key"] is True
    assert admin_auth.meter_usage(conn, owner_via_key, TOKENS)[0] is True
    ok, message = admin_auth.meter_usage(conn, owner_via_key, TOKENS)
    assert ok is False and "monthly token limit" in message


def test_only_an_exact_unlimited_flag_exempts_from_metering_and_rate_limits(conn, monkeypatch):
    monkeypatch.setattr(admin_auth, "_now", lambda: 1_800_000_000)  # one rate window throughout
    admin_auth.set_saas_feature(conn, "usage_metering_enabled", True)
    admin_auth.set_saas_tier_limit(conn, "free", TOKENS)
    for flags in ("notunlimited", "unlimited-trial developer"):
        conn.execute("UPDATE accounts SET dev_flags=? WHERE username='alice'", (flags,))
        conn.execute("DELETE FROM usage_stats")
        conn.execute("DELETE FROM account_rate")
        conn.commit()
        alice = admin_auth.public_account(conn, "alice")
        assert admin_auth.meter_usage(conn, alice, TOKENS)[0] is True, flags
        assert admin_auth.meter_usage(conn, alice, TOKENS)[0] is False, flags
        results = [admin_auth.rate_limit(conn, alice)[0] for _ in range(admin_auth.DEFAULT_RATE_LIMIT + 1)]
        assert results[-1] is False, flags
    conn.execute("UPDATE accounts SET dev_flags='developer, unlimited' WHERE username='alice'")
    conn.commit()
    alice = admin_auth.public_account(conn, "alice")
    for _ in range(3):
        assert admin_auth.meter_usage(conn, alice, TOKENS) == (True, "")
        assert admin_auth.rate_limit(conn, alice) == (True, "")


def test_toggling_metering_off_again_stops_metering(conn):
    admin_auth.set_saas_feature(conn, "usage_metering_enabled", True)
    admin_auth.set_saas_tier_limit(conn, "free", TOKENS)
    alice = admin_auth.public_account(conn, "alice")
    assert admin_auth.meter_usage(conn, alice, TOKENS)[0] is True
    assert admin_auth.meter_usage(conn, alice, TOKENS)[0] is False
    admin_auth.set_saas_feature(conn, "usage_metering_enabled", False)
    assert admin_auth.meter_usage(conn, alice, TOKENS) == (True, "")


def test_unknown_saas_switches_and_bool_or_negative_limits_are_refused(conn):
    with pytest.raises(ValueError):
        admin_auth.set_saas_feature(conn, "not_a_feature", True)
    with pytest.raises(ValueError):
        admin_auth.set_saas_tier_limit(conn, "free", -1)
    with pytest.raises(ValueError):
        admin_auth.set_saas_tier_limit(conn, "free", True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        admin_auth.set_saas_tier_limit(conn, "bad tier", 100)
    with pytest.raises(ValueError):
        admin_auth.set_api_key_ttl(conn, True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        admin_auth.set_api_key_ttl(conn, -5)


def test_concurrent_metering_cannot_overspend_budget(tmp_path):
    db = tmp_path / "accounts.db"
    setup = memory_store.connect(str(db))
    try:
        admin_auth.register(setup, "owner", "password123")
        admin_auth.register(setup, "alice", "password123")
        admin_auth.set_saas_feature(setup, "usage_metering_enabled", True)
        admin_auth.set_saas_tier_limit(setup, "free", TOKENS * 3)
        alice = admin_auth.public_account(setup, "alice")
    finally:
        setup.close()

    barrier = threading.Barrier(6)
    outcomes = []
    lock = threading.Lock()

    def worker():
        c = memory_store.connect(str(db))
        try:
            barrier.wait()
            ok, _ = admin_auth.meter_usage(c, alice, TOKENS)
            with lock:
                outcomes.append(ok)
        finally:
            c.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count(True) == 3
    assert outcomes.count(False) == 3
    check = memory_store.connect(str(db))
    try:
        used = check.execute("SELECT monthly_count FROM usage_stats WHERE username='alice'").fetchone()[0]
        assert used == TOKENS * 3
    finally:
        check.close()


def test_per_minute_rate_limit_is_independent_of_the_metering_switch(conn):
    alice = admin_auth.public_account(conn, "alice")
    assert admin_auth.saas_config(conn).is_feature_enabled("usage_metering_enabled") is False
    ok, message = admin_auth.rate_limit(conn, alice, cost=admin_auth.DEFAULT_RATE_LIMIT + 1)
    assert ok is False and "rate limit exceeded" in message
    assert admin_auth.rate_limit(conn, None) == (True, "")


def _calls_named(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node.lineno for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == name]


def test_account_and_saas_modules_have_no_debug_prints():
    for relative in ("runtime/admin_auth.py", "sonder_runtime/application/config/saas_config.py"):
        assert _calls_named(ROOT / relative, "print") == [], relative


def test_chat_route_has_no_mock_tier_or_debug_print():
    source = (ROOT / "sonder_runtime/interfaces/http/serve.py").read_text(encoding="utf-8")
    assert "setdefault('tier'" not in source and 'setdefault("tier"' not in source
    assert "DEBUG: Calling rate_limit" not in source
