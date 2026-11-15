"""POST /v1/sonder/login must feed the auth-failure limiter (public-mode slice P3).

On origin/main a wrong password returns 401 without ``record_auth_failure``,
so the token-bucket limiter never throttles password guessing, and an unknown
username skips PBKDF2 so response time reveals which names exist.  These tests
drive the real HTTP handler and assert behaviour only: status codes, the typed
429 envelope, ``Retry-After``, and a spy on the password-hash function (never
wall time).
"""
from __future__ import annotations

import ipaddress
import json
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

import admin_auth
import memory_store
import sonder_runtime.adapters.web.lifecycle as sonder_lifecycle
import sonder_runtime.interfaces.http.serve as sonder_serve

pytestmark = pytest.mark.integration

USER = "operator"
GOOD = "correct-horse-battery"
BAD = "definitely-wrong-pw"
# Per-IP burst comes from the existing limiter configuration.
BURST = sonder_lifecycle._AUTH_BUCKET_CAPACITY
IP_A = "203.0.113.10"
IP_B = "203.0.113.20"
IP_C = "203.0.113.30"


class _Clock:
    """Monotonic/wall offset so tests pass a backoff window without sleeping."""

    def __init__(self):
        self.offset = 0.0
        self._mono = time.monotonic
        self._wall = time.time

    def advance(self, seconds):
        self.offset += seconds

    def monotonic(self):
        return self._mono() + self.offset

    def time(self):
        return self._wall() + self.offset


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(time, "monotonic", c.monotonic)
    monkeypatch.setattr(time, "time", c.time)
    return c


@pytest.fixture()
def login_server(tmp_path, monkeypatch, clock):
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    sonder_lifecycle.reset_for_tests()
    monkeypatch.setattr(sonder_serve, "API_KEY", "")
    monkeypatch.setattr(sonder_serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(sonder_serve, "REQUIRE_ACCOUNT", False)
    # Declare a loopback proxy so X-Forwarded-For selects the client IP
    # through the existing _client_ip (trusted-proxy aware).
    monkeypatch.setattr(sonder_serve, "TLS_TERMINATED_BY_PROXY", True)
    monkeypatch.setattr(
        sonder_serve, "_TRUSTED_PROXY_NETWORKS",
        (ipaddress.ip_network("127.0.0.0/8"),),
    )
    db_path = tmp_path / "accounts.db"
    monkeypatch.setattr(
        sonder_serve._legacy_runtime(), "_open_db",
        lambda: memory_store.connect(db_path),
    )
    _register(USER)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), sonder_serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    sonder_lifecycle.reset_for_tests()


def _register(username):
    conn = sonder_serve._legacy_runtime()._open_db()
    try:
        admin_auth.register(conn, username, GOOD, trusted_local=True)
    finally:
        conn.close()


def _login(base, username, password, ip):
    request = urllib.request.Request(
        base + "/v1/sonder/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json", "X-Forwarded-For": ip},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read()), dict(response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else {}, dict(error.headers)


def test_fifty_wrong_passwords_from_one_ip_get_throttled(login_server):
    statuses = [_login(login_server, USER, BAD, IP_A)[0] for _ in range(50)]
    assert 429 in statuses
    # The first BURST guesses are real attempts (401); throttling starts after.
    assert statuses[:BURST] == [401] * BURST
    assert set(statuses[BURST:]) == {429}


def test_throttled_login_uses_typed_429_with_retry_after(login_server):
    for _ in range(BURST):
        _login(login_server, USER, BAD, IP_A)
    status, body, headers = _login(login_server, USER, BAD, IP_A)
    assert status == 429
    assert body["error"]["code"] == "AUTH_RATE_LIMITED"
    assert body["error"]["retryable"] is True
    retry_after = {k.lower(): v for k, v in headers.items()}["retry-after"]
    assert int(retry_after) >= 1


def test_correct_password_is_also_refused_while_ip_is_limited(login_server):
    for _ in range(BURST):
        _login(login_server, USER, BAD, IP_A)
    status, body, _ = _login(login_server, USER, GOOD, IP_A)
    assert status == 429
    assert "token" not in body


def test_other_ip_is_not_affected_by_per_ip_limit(login_server):
    for _ in range(BURST + 5):
        _login(login_server, USER, BAD, IP_A)
    # A different account keeps the per-username counter out of the picture.
    _register("other")
    status, body, _ = _login(login_server, "other", GOOD, IP_B)
    assert status == 200
    assert body["token"]


def test_per_username_backoff_applies_across_ips(login_server):
    # Each IP stays under its own per-IP burst; together they exceed any
    # sane per-account budget for one username.
    for ip in (IP_A, IP_B, IP_C):
        for _ in range(BURST - 2):
            _login(login_server, USER, BAD, ip)
    status, body, headers = _login(login_server, USER, GOOD, "203.0.113.99")
    assert status == 429
    assert body["error"]["code"] == "AUTH_RATE_LIMITED"
    assert "retry-after" in {k.lower() for k in headers}


def test_username_backoff_is_a_window_not_a_permanent_lock(login_server, clock):
    for ip in (IP_A, IP_B, IP_C):
        for _ in range(BURST - 2):
            _login(login_server, USER, BAD, ip)
    assert _login(login_server, USER, GOOD, "203.0.113.99")[0] == 429
    clock.advance(24 * 3600)
    status, body, _ = _login(login_server, USER, GOOD, "203.0.113.99")
    assert status == 200
    assert body["token"]


def test_correct_password_succeeds_after_ip_window_passes(login_server, clock):
    for _ in range(BURST + 3):
        _login(login_server, USER, BAD, IP_A)
    assert _login(login_server, USER, GOOD, IP_A)[0] == 429
    clock.advance(24 * 3600)
    status, body, _ = _login(login_server, USER, GOOD, IP_A)
    assert status == 200
    assert body["token"]


def test_unknown_username_failures_also_count_against_the_ip(login_server):
    statuses = [
        _login(login_server, f"ghost{i}", BAD, IP_A)[0] for i in range(BURST + 5)
    ]
    assert statuses[:BURST] == [401] * BURST
    assert set(statuses[BURST:]) == {429}


def test_unknown_user_and_wrong_password_both_run_the_hash(login_server, monkeypatch):
    calls = []
    real = admin_auth._hash_password

    def spy(password, salt=None):
        calls.append(password)
        return real(password, salt)

    monkeypatch.setattr(admin_auth, "_hash_password", spy)
    status_known, _, _ = _login(login_server, USER, BAD, IP_A)
    known_calls = len(calls)
    calls.clear()
    status_unknown, _, _ = _login(login_server, "no-such-user", BAD, IP_B)
    assert status_known == status_unknown == 401
    assert known_calls == 1
    assert len(calls) == known_calls


def test_unknown_user_hashes_in_admin_auth_login_directly(monkeypatch):
    """admin_login (MCP/HTTP tool) calls admin_auth.login: same hash cost."""
    calls = []
    real = admin_auth._hash_password

    def spy(password, salt=None):
        calls.append(password)
        return real(password, salt)

    monkeypatch.setattr(admin_auth, "_hash_password", spy)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    admin_auth.init(conn)
    with pytest.raises(ValueError):
        admin_auth.login(conn, "ghost-user", "whatever-password")
    assert len(calls) == 1


def test_successful_logins_still_work_below_the_limit(login_server):
    for _ in range(3):
        status, body, _ = _login(login_server, USER, GOOD, IP_A)
        assert status == 200
        assert body["ok"] is True and body["token"]


def test_wrong_password_below_limit_keeps_the_generic_401(login_server):
    status, body, _ = _login(login_server, USER, BAD, IP_A)
    assert status == 401
    assert body == {"ok": False, "message": "invalid username or password"}
