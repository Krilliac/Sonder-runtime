"""Host-header allowlist (DNS rebinding) and X-Forwarded-For trust on the HTTP adapter."""

from contextlib import contextmanager
import http.client
import ipaddress
import json
import threading

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.interfaces.http.host_policy import (
    HOST_CREDENTIALED, HOST_TRUSTED, forwarded_client_ip, host_allowed, host_decision,
    machine_host_names, parse_host_header,
)
from sonder_runtime.platform.config import ConfigError, load_config


LOOPBACK_NETS = (ipaddress.ip_network("127.0.0.1/32"), ipaddress.ip_network("::1/128"))


class _Metric:
    def labels(self, **_kwargs):
        return self

    def inc(self, *_args, **_kwargs):
        return None

    def observe(self, *_args, **_kwargs):
        return None


class _Metrics:
    requests_total = _Metric()
    request_duration_seconds = _Metric()


class _Lifecycle:
    metrics = _Metrics()

    def __init__(self):
        self.failures = []

    def idempotent(self, _key, factory, **_kwargs):
        return factory()

    def auth_attempt_allowed(self, _peer):
        return True

    def record_auth_failure(self, peer, reason):
        self.failures.append(peer)

    def live_payload(self):
        return {"status": "alive"}

    def version_payload(self):
        return {"version": "test"}


API_KEY = "k" * 40
MACHINE_NAMES = machine_host_names("mypc", "mypc.corp.example")


@contextmanager
def _server(monkeypatch, *, allowed_hosts=(), api_key=""):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "API_KEY", api_key)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key" if api_key else "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "_MACHINE_HOST_NAMES", MACHINE_NAMES)
    monkeypatch.setattr(ts, "HOST", "127.0.0.1")
    if hasattr(ts, "_parse_allowed_hosts"):
        monkeypatch.setattr(ts, "ALLOWED_HOSTS", ts._parse_allowed_hosts(allowed_hosts))
    lifecycle = _Lifecycle()
    monkeypatch.setattr(ts.sonder_lifecycle, "get", lambda: lifecycle)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(port, path, host):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest("GET", path, skip_host=True)
        if host is not None:
            conn.putheader("Host", host)
        conn.endheaders()
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def test_rebinding_host_is_refused_before_routing(monkeypatch):
    with _server(monkeypatch) as port:
        status, body = _get(port, "/live", "rebind.attacker.example:%d" % port)
        assert status == 421
        error = json.loads(body)["error"]
        assert error["code"] == "HOST_NOT_ALLOWED"
        # The body names the remedy, including the setting to change.
        assert "allowed_hosts" in error["remedy"]
        assert "SONDER_ALLOWED_HOSTS" in error["remedy"]
        # A rebinding name is refused on any route, not just /live.
        status, _ = _get(port, "/v1/sonder/status", "rebind.attacker.example:%d" % port)
        assert status == 421


def test_dns_rebinding_canary_local_open_refuses_attacker_name(monkeypatch):
    """Canary: an unauthenticated listener never answers an attacker-chosen name.

    Every relaxation below (addresses on any port, machine names, any name
    behind credentials) must leave this exact case refused.
    """
    with _server(monkeypatch) as port:
        for host in ("evil.example", "evil.example:%d" % port, "evil.example:80",
                     "EVIL.example.", "mypc.evil.example"):
            status, body = _get(port, "/live", host)
            assert status == 421, host
            assert json.loads(body)["error"]["code"] == "HOST_NOT_ALLOWED"


@pytest.mark.parametrize("host_template", [
    "127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}", "127.0.0.1", "LOCALHOST:{port}",
    "app.localhost:{port}",
])
def test_loopback_hosts_are_served(monkeypatch, host_template):
    with _server(monkeypatch) as port:
        status, _ = _get(port, "/live", host_template.format(port=port))
        assert status == 200


@pytest.mark.parametrize("host", [
    "10.0.2.2:11435",          # Android emulator's alias for the host PC
    "192.168.1.20:11435",      # LAN address
    "192.168.1.20",            # no port
    "100.101.102.103:8443",    # Tailscale address through a port forward
    "[fd7a:115c:a1e0::1]:11435",
    "127.0.0.1:18080",         # adb reverse / ssh -L onto another local port
    "localhost:18080",
])
def test_phone_addresses_are_served_on_any_port_even_local_open(monkeypatch, host):
    with _server(monkeypatch) as port:
        assert _get(port, "/live", host)[0] == 200


@pytest.mark.parametrize("host", ["mypc", "mypc:11435", "MYPC.local:8080", "mypc.corp.example"])
def test_machine_own_names_are_served_even_local_open(monkeypatch, host):
    with _server(monkeypatch) as port:
        assert _get(port, "/live", host)[0] == 200


@pytest.mark.parametrize("host", ["mypc.tail1234.ts.net", "sonder.example.com:443", "evil.example"])
def test_any_name_is_served_when_credentials_are_required(monkeypatch, host):
    # MagicDNS names, proxy names and yes, attacker names: a rebinding page
    # holds no credentials, so the listener's auth is what refuses it.
    with _server(monkeypatch, api_key=API_KEY) as port:
        assert _get(port, "/live", host)[0] == 200


def test_credentialed_name_does_not_get_loopback_peer_exemptions(monkeypatch):
    """A rebinding page runs in a local browser: a loopback peer is no proof."""
    with _server(monkeypatch, api_key=API_KEY) as port:
        # /version needs auth except from a loopback peer through a trusted name.
        assert _get(port, "/version", "127.0.0.1:%d" % port)[0] == 200
        assert _get(port, "/version", "mypc.local")[0] == 200
        status, _ = _get(port, "/version", "evil.example:%d" % port)
        assert status == 401
        # The unauthenticated local log page is not reachable through it either.
        assert _get(port, "/v1/local/server-log", "evil.example")[0] == 404


def test_unspecified_address_is_refused(monkeypatch):
    with _server(monkeypatch) as port:
        assert _get(port, "/live", "0.0.0.0:%d" % port)[0] == 421
        assert _get(port, "/live", "[::]:%d" % port)[0] == 421


def test_duplicate_host_headers_are_refused(monkeypatch):
    with _server(monkeypatch, api_key=API_KEY) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        try:
            conn.putrequest("GET", "/live", skip_host=True)
            conn.putheader("Host", "127.0.0.1")
            conn.putheader("Host", "evil.example")
            conn.endheaders()
            assert conn.getresponse().status == 421
        finally:
            conn.close()


def test_configured_public_host_is_served(monkeypatch):
    with _server(monkeypatch, allowed_hosts=("sonder.example.internal",)) as port:
        assert _get(port, "/live", "sonder.example.internal")[0] == 200
        assert _get(port, "/live", "other.example.internal")[0] == 421


def test_missing_host_header_is_accepted_for_non_browser_clients(monkeypatch):
    with _server(monkeypatch) as port:
        assert _get(port, "/live", None)[0] == 200


def test_rejected_host_warning_names_the_host_and_is_rate_limited(monkeypatch, caplog):
    monkeypatch.setattr(ts, "_REJECTED_HOST_LOG", ts.OrderedDict())
    with caplog.at_level("WARNING", logger=ts._serve_logger.name):
        ts._log_rejected_host("mypc.lan:11435")
        ts._log_rejected_host("mypc.lan:11435")
    warnings = [r.getMessage() for r in caplog.records if "HOST_NOT_ALLOWED" in r.getMessage()]
    assert len(warnings) == 1
    assert "mypc.lan" in warnings[0] and "allowed_hosts" in warnings[0]


def test_rejected_host_warnings_are_capped_across_rotating_names(monkeypatch, caplog):
    # A rebinding page can rotate attacker-chosen names; the per-name limit
    # alone would log once per request.
    monkeypatch.setattr(ts, "_REJECTED_HOST_LOG", ts.OrderedDict())
    with caplog.at_level("WARNING", logger=ts._serve_logger.name):
        for index in range(50):
            ts._log_rejected_host("r%d.rebind.example" % index)
    warnings = [r for r in caplog.records if "HOST_NOT_ALLOWED" in r.getMessage()]
    assert len(warnings) == ts._REJECTED_HOST_LOG_MAX_NAMES_PER_INTERVAL


def test_machine_names_are_computed_once_with_a_bounded_fqdn_lookup(monkeypatch):
    import socket
    import time as _time

    monkeypatch.setattr(socket, "gethostname", lambda: "Desk-PC")

    def slow_fqdn(_name):
        _time.sleep(2)
        return "desk-pc.never.example"

    monkeypatch.setattr(socket, "getfqdn", slow_fqdn)
    started = _time.monotonic()
    names = ts._compute_machine_host_names(timeout=0.05)
    assert _time.monotonic() - started < 1.5
    assert names == {"desk-pc", "desk-pc.local"}

    calls = []
    monkeypatch.setattr(ts, "_MACHINE_HOST_NAMES", None)
    monkeypatch.setattr(ts, "_compute_machine_host_names",
                        lambda: calls.append("c") or frozenset({"x"}))
    assert ts._machine_host_names() == {"x"}
    assert ts._machine_host_names() == {"x"}
    assert calls == ["c"]


def test_host_policy_pure_rules():
    assert parse_host_header("[::1]:8080") == ("::1", 8080)
    assert parse_host_header("::1") is None
    assert parse_host_header("a_b.example") is None
    assert parse_host_header("x:0") is None
    # Addresses: any port, whatever the auth mode.
    assert host_decision("192.168.1.20:11435") == HOST_TRUSTED
    assert host_decision("10.0.2.2:8080") == HOST_TRUSTED
    assert host_decision("0.0.0.0:11435", credentials_required=True) is None
    assert host_decision("[::]:11435") is None
    assert host_decision("[::ffff:0.0.0.0]:11435", credentials_required=True) is None
    # Names: refused without credentials, credentialed with them.
    assert host_decision("rebind.example:11435") is None
    assert host_decision("rebind.example:11435", credentials_required=True) == HOST_CREDENTIALED
    assert host_allowed("rebind.example", credentials_required=True)
    assert not host_allowed("rebind.example")
    assert not host_allowed("bad_name", credentials_required=True)
    # Machine names and allowlist entries are trusted.
    names = machine_host_names("MyPC", "mypc.corp.example.")
    assert names == {"mypc", "mypc.local", "mypc.corp.example"}
    assert host_decision("mypc.local:9", local_names=names) == HOST_TRUSTED
    assert machine_host_names("10.0.0.5", "") == frozenset()
    assert host_decision("public.example:443", allowed_hosts=("public.example:443",)) == HOST_TRUSTED
    assert host_decision("public.example:8443", allowed_hosts=("public.example:443",)) is None
    assert host_decision("public.example:8443", allowed_hosts=("public.example:443",),
                         credentials_required=True) == HOST_CREDENTIALED


def test_allowed_hosts_config_is_validated():
    config = load_config(None, env={"SONDER_ALLOWED_HOSTS": "sonder.example.internal:443, [::1]:9"})
    assert config.server.allowed_hosts == ("sonder.example.internal:443", "[::1]:9")
    with pytest.raises(ConfigError) as refused:
        load_config(None, env={"SONDER_ALLOWED_HOSTS": "bad host"})
    assert "allowed_hosts" in str(refused.value)


# -- X-Forwarded-For --------------------------------------------------------

def test_forwarded_for_ignored_without_declared_proxy():
    assert forwarded_client_ip(
        "127.0.0.1", "10.9.1.1", proxy_declared=False, trusted_networks=LOOPBACK_NETS,
    ) == "127.0.0.1"


def test_forwarded_for_uses_rightmost_untrusted_hop_via_declared_proxy():
    assert forwarded_client_ip(
        "127.0.0.1", "10.9.1.1", proxy_declared=True, trusted_networks=LOOPBACK_NETS,
    ) == "10.9.1.1"
    # A client-supplied prefix cannot choose the address.
    assert forwarded_client_ip(
        "127.0.0.1", "6.6.6.6, 203.0.113.9", proxy_declared=True,
        trusted_networks=LOOPBACK_NETS,
    ) == "203.0.113.9"
    assert forwarded_client_ip(
        "127.0.0.1", "not-an-ip", proxy_declared=True, trusted_networks=LOOPBACK_NETS,
    ) == "127.0.0.1"
    # An untrusted peer never delegates to the header.
    assert forwarded_client_ip(
        "198.51.100.4", "10.9.1.1", proxy_declared=True, trusted_networks=LOOPBACK_NETS,
    ) == "198.51.100.4"


def test_rotating_forwarded_for_from_loopback_does_not_escape_the_limiter(monkeypatch):
    """Every failure is charged to the raw loopback peer when no proxy is declared."""
    monkeypatch.setattr(ts, "TLS_TERMINATED_BY_PROXY", False)
    monkeypatch.setattr(ts, "_TRUSTED_PROXY_NETWORKS", LOOPBACK_NETS)
    seen = []
    for n in range(5):
        handler = ts.Handler.__new__(ts.Handler)
        handler.client_address = ("127.0.0.1", 50000 + n)
        handler.headers = {"X-Forwarded-For": "10.9.%d.1" % n}
        seen.append(handler._client_ip())
    assert seen == ["127.0.0.1"] * 5
