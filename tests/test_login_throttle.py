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
from sonder_runtime.adapters.security import login_throttle
from sonder_runtime.domain.login_throttle import LoginThrottlePolicy
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
# Explicit per-username policy (not the shipped default): 3 IPs x (BURST-2)
# failures must exceed it while each IP stays under its own burst.
POLICY = LoginThrottlePolicy(threshold=20, decay_seconds=30.0)


class _Clock:
    """Monotonic/wall offset so tests pass a backoff window without sleeping."""

    def __init__(self):
        # Frozen at construction: only advance() moves time, so a slow test
        # run cannot refill the per-IP bucket behind the assertions.
        self.offset = 0.0
        self._mono = time.monotonic()
        self._wall = time.time()

    def advance(self, seconds):
        self.offset += seconds

    def monotonic(self):
        return self._mono + self.offset

    def time(self):
        return self._wall + self.offset


@pytest.fixture()
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(time, "monotonic", c.monotonic)
    monkeypatch.setattr(time, "time", c.time)
    return c


class _RoomyServer(ThreadingHTTPServer):
    # The concurrent-burst test opens 40 connections at once.
    request_queue_size = 128


@pytest.fixture()
def login_server(tmp_path, monkeypatch, clock):
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    sonder_lifecycle.reset_for_tests()
    login_throttle.configure(POLICY)
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
    httpd = _RoomyServer(("127.0.0.1", 0), sonder_serve.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()
    httpd.server_close()
    sonder_lifecycle.reset_for_tests()
    login_throttle.configure(None)


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


def _header(headers, name):
    return {k.lower(): v for k, v in headers.items()}.get(name)


def _post(base, path, payload, ip, extra_headers=None):
    headers = {"Content-Type": "application/json", "X-Forwarded-For": ip}
    headers.update(extra_headers or {})
    request = urllib.request.Request(
        base + path, data=json.dumps(payload).encode(), headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, json.loads(response.read() or b"{}"), dict(response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, json.loads(raw) if raw else {}, dict(error.headers)


def _login_no_forwarding(base, username, password):
    """A direct loopback client: no X-Forwarded-For, so the peer is the client."""
    request = urllib.request.Request(
        base + "/v1/sonder/login",
        data=json.dumps({"username": username, "password": password}).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status
    except urllib.error.HTTPError as error:
        error.read()
        return error.code


def _spray_until_window(base):
    for ip in (IP_A, IP_B, IP_C):
        for _ in range(BURST - 2):
            _login(base, USER, BAD, ip)


def test_concurrent_burst_from_one_ip_cannot_overdraw_the_limiter(login_server):
    """Check-and-consume is one step: queued requests cannot all pass the check."""
    count = 40
    results = [None] * count
    barrier = threading.Barrier(count)

    def attempt(index):
        barrier.wait()
        # Distinct usernames keep the per-username stage out of the picture.
        results[index] = _login(login_server, f"spray{index}", BAD, IP_A)[0]

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count(401) == BURST
    assert results.count(429) == count - BURST


def test_concurrent_burst_does_not_exhaust_budget_for_successful_logins(login_server):
    """Successful logins refund their reservation, so they never drain the IP."""
    for _ in range(3 * BURST):
        assert _login(login_server, USER, GOOD, IP_A)[0] == 200
    assert _login(login_server, USER, BAD, IP_A)[0] == 401


def test_sustained_remote_attacker_cannot_keep_a_familiar_operator_out(login_server, clock):
    assert _login(login_server, USER, GOOD, IP_B)[0] == 200  # operator's usual IP
    _spray_until_window(login_server)
    attacker = "198.51.100.77"
    for _ in range(4):
        clock.advance(31)  # the attacker takes every slot the decay opens
        _login(login_server, USER, BAD, attacker)
        assert _login(login_server, USER, BAD, attacker)[0] == 429
        # The operator is not held out, and is not spending the attacker's budget.
        assert _login(login_server, USER, GOOD, IP_B)[0] == 200
        _spray_until_window(login_server)


def test_unfamiliar_remote_address_stays_inside_the_window(login_server):
    _spray_until_window(login_server)
    assert _login(login_server, USER, GOOD, "198.51.100.200")[0] == 429


def test_loopback_operator_is_never_held_out_by_remote_failures(login_server):
    _spray_until_window(login_server)
    assert _login_no_forwarding(login_server, USER, GOOD) == 200


def test_username_window_reports_its_real_retry_after(login_server):
    _spray_until_window(login_server)  # refusals stop counting at the threshold (20): one slot
    status, body, headers = _login(login_server, USER, GOOD, "198.51.100.201")
    assert status == 429
    assert body["error"]["code"] == "AUTH_RATE_LIMITED"
    assert int(_header(headers, "retry-after")) == 30  # one decay slot


def test_username_refusals_do_not_spend_the_callers_ip_budget(login_server):
    _spray_until_window(login_server)
    for _ in range(3 * BURST):
        assert _login(login_server, USER, GOOD, "198.51.100.202")[0] == 429
    # The IP budget is untouched: a different account still logs in from it.
    _register("someone-else")
    assert _login(login_server, "someone-else", GOOD, "198.51.100.202")[0] == 200


def test_banned_account_with_correct_password_is_a_counted_failure(login_server):
    conn = sonder_serve._legacy_runtime()._open_db()
    try:
        conn.execute("UPDATE accounts SET banned=1 WHERE username=?", (USER,))
        conn.commit()
    finally:
        conn.close()
    statuses = [_login(login_server, USER, GOOD, IP_A)[0] for _ in range(BURST + 3)]
    assert statuses[:BURST] == [401] * BURST
    assert set(statuses[BURST:]) == {429}


def test_register_denials_are_throttled_per_ip(login_server):
    def register(index):
        return _post(
            login_server, "/v1/sonder/register",
            {"username": f"newbie{index}", "password": "another-long-password"},
            IP_A, {"X-Sonder-Bootstrap-Secret": "wrong-secret"},
        )

    statuses = [register(i)[0] for i in range(BURST + 5)]
    assert statuses[:BURST] == [403] * BURST
    status, body, headers = register(99)
    assert status == 429
    assert body["error"]["code"] == "AUTH_RATE_LIMITED"
    assert int(_header(headers, "retry-after")) >= 1
    # Another address is unaffected.
    assert register_status_for(login_server, IP_B) == 403


def register_status_for(base, ip):
    return _post(
        base, "/v1/sonder/register",
        {"username": "elsewhere", "password": "another-long-password"},
        ip, {"X-Sonder-Bootstrap-Secret": "wrong-secret"},
    )[0]


def test_l04_duplicate_registration_normalizes_in_public_hosting_and_preserves_admin_409(
    monkeypatch, tmp_path,
):
    from sonder_runtime.adapters.web import login_throttle_http

    sonder_lifecycle.reset_for_tests()
    login_throttle_http.reset_registration_throttle()
    db_path = tmp_path / "l04.db"

    class _Runtime:
        def _open_db(self):
            return memory_store.connect(db_path)

    class _FakeHandler:
        def __init__(self, ip: str = "198.51.100.55"):
            self.ip = ip
            self.headers = {}
            self.sent = []

        def _client_ip(self):
            return self.ip

        def _correlation(self):
            return "corr-l04"

        def _auth_reserve_refused(self):
            if not sonder_lifecycle.get().reserve_auth_attempt(self.ip):
                login_throttle_http.send_throttled(self, 2)
                return True
            return False

        def _send_json_payload(self, payload, status=200, headers=None):
            self.sent.append((status, payload, headers or {}))

        def log_error(self, *args):
            return

    rt = _Runtime()
    conn = rt._open_db()
    try:
        admin = admin_auth.register(conn, "existing_user", "Str0ng!Pass#2026", trusted_local=True)
    finally:
        conn.close()

    # 1. Public hosting: duplicate registration returns generic 400 (no username disclosure)
    # and spends the auth-failure budget instead of refunding.
    h_pub = _FakeHandler(ip="198.51.100.55")
    login_throttle_http.handle_register(
        h_pub,
        {"username": "existing_user", "password": "Another!Str0ng#2026"},
        {"authorized": False, "account": None},
        runtime=rt,
        admin_auth=admin_auth,
        allow_registration=True,
        created_message=lambda acc: f"created {acc['username']}",
        public_hosting=True,
    )
    status_pub, body_pub, _ = h_pub.sent[-1]
    assert status_pub == 400
    assert body_pub == {"ok": False, "message": "registration could not be completed"}
    assert "exists" not in body_pub["message"].lower()

    # 2. Private admin install keeps the documented 409 contract with zero extra metadata.
    h_priv = _FakeHandler(ip="198.51.100.56")
    login_throttle_http.handle_register(
        h_priv,
        {"username": "existing_user", "password": "Another!Str0ng#2026"},
        {"authorized": True, "account": admin},
        runtime=rt,
        admin_auth=admin_auth,
        allow_registration=True,
        created_message=lambda acc: f"created {acc['username']}",
        public_hosting=False,
    )
    status_priv, body_priv, _ = h_priv.sent[-1]
    assert status_priv == 409
    assert body_priv == {"ok": False, "message": "account already exists"}

    # 3. Configurable generic-error env mode normalizes private installs too.
    monkeypatch.setenv("SONDER_GENERIC_REGISTER_ERRORS", "1")
    h_env = _FakeHandler(ip="198.51.100.57")
    login_throttle_http.handle_register(
        h_env,
        {"username": "existing_user", "password": "Another!Str0ng#2026"},
        {"authorized": True, "account": admin},
        runtime=rt,
        admin_auth=admin_auth,
        allow_registration=True,
        created_message=lambda acc: f"created {acc['username']}",
        public_hosting=False,
    )
    status_env, body_env, _ = h_env.sent[-1]
    assert status_env == 400
    assert body_env == {"ok": False, "message": "registration could not be completed"}
    sonder_lifecycle.reset_for_tests()
    login_throttle_http.reset_registration_throttle()
