"""Host-header allowlist (DNS rebinding) and X-Forwarded-For trust on the HTTP adapter."""

from contextlib import contextmanager
import http.client
import ipaddress
import json
import threading

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.interfaces.http.host_policy import (
    forwarded_client_ip, host_allowed, parse_host_header,
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


@contextmanager
def _server(monkeypatch, *, allowed_hosts=()):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
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
        assert json.loads(body)["error"]["code"] == "HOST_NOT_ALLOWED"
        # A rebinding name is refused on any route, not just /live.
        status, _ = _get(port, "/v1/sonder/status", "rebind.attacker.example:%d" % port)
        assert status == 421


@pytest.mark.parametrize("host_template", [
    "127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}", "127.0.0.1", "LOCALHOST:{port}",
])
def test_loopback_hosts_with_bound_port_are_served(monkeypatch, host_template):
    with _server(monkeypatch) as port:
        status, _ = _get(port, "/live", host_template.format(port=port))
        assert status == 200


def test_loopback_name_with_wrong_port_is_refused(monkeypatch):
    with _server(monkeypatch) as port:
        status, _ = _get(port, "/live", "127.0.0.1:%d" % (port + 1 if port < 65535 else port - 1))
        assert status == 421


def test_unspecified_address_is_refused_on_loopback_bind(monkeypatch):
    with _server(monkeypatch) as port:
        status, _ = _get(port, "/live", "0.0.0.0:%d" % port)
        assert status == 421


def test_configured_public_host_is_served(monkeypatch):
    with _server(monkeypatch, allowed_hosts=("sonder.example.internal",)) as port:
        assert _get(port, "/live", "sonder.example.internal")[0] == 200
        assert _get(port, "/live", "other.example.internal")[0] == 421


def test_missing_host_header_is_accepted_for_non_browser_clients(monkeypatch):
    with _server(monkeypatch) as port:
        assert _get(port, "/live", None)[0] == 200


def test_host_policy_pure_rules():
    assert parse_host_header("[::1]:8080") == ("::1", 8080)
    assert parse_host_header("::1") is None
    assert parse_host_header("a_b.example") is None
    assert parse_host_header("x:0") is None
    # Non-loopback bind: remote clients reach the listener by address.
    assert host_allowed("192.168.1.20:11435", bind_host="0.0.0.0", bound_port=11435)
    assert not host_allowed("rebind.example:11435", bind_host="0.0.0.0", bound_port=11435)
    assert not host_allowed("192.168.1.20:11435", bind_host="127.0.0.1", bound_port=11435)
    assert host_allowed("public.example:443", bind_host="127.0.0.1", bound_port=11435,
                        allowed_hosts=("public.example:443",))
    assert not host_allowed("public.example:8443", bind_host="127.0.0.1", bound_port=11435,
                            allowed_hosts=("public.example:443",))


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
