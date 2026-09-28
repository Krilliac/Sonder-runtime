"""Transport hardening for the OpenAI-compatible gateway's default transports.

Real loopback HTTP servers stand in for the endpoint, an ambient proxy and a
redirect target, so these tests exercise urllib's actual proxy and redirect
handling rather than a fake: a loopback endpoint must never be reached through
an environment proxy, an authenticated POST must never follow a redirect, a
plaintext non-loopback endpoint is refused unless it is a private address the
operator explicitly trusted, and a successful body has a byte ceiling.
"""
from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest

from sonder_runtime.adapters.inference import openai_compat_gateway as gateway_module
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import (
    DependencyUnavailable,
    Forbidden,
    InvalidInput,
)

_KEY = "sk-test-transport-fixture"


def _ctx(*, cloud=False):
    return local_owner_context(
        correlation_id="req_transport", cloud_allowed=cloud, timeout_seconds=20.0
    )


def _chat(text="ok", pad=0):
    return json.dumps({
        "choices": [{"message": {"role": "assistant", "content": text}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        "pad": "x" * pad,
    }).encode("utf-8")


def _handler(seen, respond):
    class Handler(http.server.BaseHTTPRequestHandler):
        def _any(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen.append({
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
            })
            status, headers, body = respond(self)
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _any
        do_POST = _any

        def log_message(self, *_args):
            pass

    return Handler


@contextmanager
def _serve(seen, respond):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(seen, respond))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _ok(_handler_self):
    return 200, {"Content-Type": "application/json"}, _chat("from-target")


@pytest.fixture
def ambient_proxy(monkeypatch):
    """An environment HTTP proxy with no loopback bypass, recording traffic."""
    seen = []
    with _serve(seen, lambda _h: (200, {"Content-Type": "application/json"},
                                  _chat("from-proxy"))) as port:
        for name in ("no_proxy", "NO_PROXY"):
            monkeypatch.delenv(name, raising=False)
        for name in ("http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY"):
            monkeypatch.setenv(name, "http://127.0.0.1:%d" % port)
        yield seen


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1"])
def test_loopback_post_bypasses_ambient_proxy(ambient_proxy, host):
    target = []
    with _serve(target, _ok) as port:
        gateway = OpenAICompatibleGateway(OpenAICompatibleConfig(
            base_url="http://%s:%d" % (host, port), api_key=_KEY, model="m",
        ))
        response = gateway.generate(ModelRequest(prompt="private", tier="code"), _ctx())

    assert response.text == "from-target"
    assert ambient_proxy == []
    assert target and target[0]["authorization"] == "Bearer " + _KEY


def test_loopback_get_bypasses_ambient_proxy(ambient_proxy):
    target = []
    with _serve(target, _ok) as port:
        gateway = OpenAICompatibleGateway(OpenAICompatibleConfig(
            base_url="http://localhost:%d" % port, api_key=_KEY, model="m",
        ))
        status, _body = gateway.get_json("/health", timeout=5.0)

    assert status == 200
    assert ambient_proxy == []
    assert len(target) == 1


def test_authenticated_post_never_follows_a_redirect():
    stolen = []
    with _serve(stolen, _ok) as thief_port:
        def redirect(_h):
            return 302, {"Location": "http://127.0.0.1:%d/steal" % thief_port}, b""

        origin = []
        with _serve(origin, redirect) as port:
            gateway = OpenAICompatibleGateway(OpenAICompatibleConfig(
                base_url="http://127.0.0.1:%d" % port, api_key=_KEY, model="m",
            ))
            with pytest.raises(DependencyUnavailable, match="HTTP 302"):
                gateway.generate(ModelRequest(prompt="private", tier="code"), _ctx())

    assert len(origin) == 1
    assert stolen == []


def test_successful_post_body_has_a_byte_ceiling(monkeypatch):
    monkeypatch.setattr(gateway_module, "POST_BODY_LIMIT", 4096, raising=False)
    target = []
    with _serve(target, lambda _h: (200, {"Content-Type": "application/json"},
                                    _chat(pad=64 * 1024))) as port:
        gateway = OpenAICompatibleGateway(OpenAICompatibleConfig(
            base_url="http://127.0.0.1:%d" % port, model="m",
        ))
        with pytest.raises(DependencyUnavailable, match="exceeds"):
            gateway.generate(ModelRequest(prompt="q", tier="code"), _ctx())


def test_body_within_the_ceiling_is_parsed(monkeypatch):
    monkeypatch.setattr(gateway_module, "POST_BODY_LIMIT", 64 * 1024, raising=False)
    with _serve([], lambda _h: (200, {"Content-Type": "application/json"},
                                _chat("fits", pad=1024))) as port:
        gateway = OpenAICompatibleGateway(OpenAICompatibleConfig(
            base_url="http://127.0.0.1:%d" % port, model="m",
        ))
        assert gateway.generate(ModelRequest(prompt="q", tier="code"), _ctx()).text == "fits"


def _recording_gateway(base_url, **config):
    calls = []

    def transport(url, payload, headers, timeout):
        calls.append(headers)
        return {"choices": [{"message": {"content": "sent"}}]}

    gateway = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url=base_url, api_key=_KEY, model="m", **config),
        transport=transport,
    )
    return gateway, calls


@pytest.mark.parametrize("base_url", [
    "http://api.example.com",
    "http://203.0.113.7:8080",
    "http://10.0.0.5:8080",  # private, but not explicitly trusted
])
def test_plaintext_non_loopback_endpoint_is_refused_even_with_cloud_consent(base_url):
    gateway, calls = _recording_gateway(base_url)
    with pytest.raises(Forbidden, match="https"):
        gateway.generate(ModelRequest(prompt="private", tier="code"), _ctx(cloud=True))
    with pytest.raises(Forbidden, match="https"):
        gateway.embed(["private"], _ctx(cloud=True))
    with pytest.raises(Forbidden, match="https"):
        gateway.get_json("/health", timeout=1.0)
    assert calls == []


def test_https_remote_endpoint_is_still_allowed_with_cloud_consent():
    gateway, calls = _recording_gateway("https://api.example.com")
    assert gateway.generate(ModelRequest(prompt="q", tier="code"), _ctx(cloud=True)).text == "sent"
    assert len(calls) == 1


def test_explicitly_trusted_private_network_may_use_plaintext():
    gateway, calls = _recording_gateway(
        "http://10.0.0.5:8080", plaintext_networks=("10.0.0.0/8",),
    )
    assert gateway.generate(ModelRequest(prompt="q", tier="code"), _ctx(cloud=True)).text == "sent"
    assert len(calls) == 1


@pytest.mark.parametrize("base_url, networks", [
    ("http://gpu-box.lan:8080", ("10.0.0.0/8",)),        # hostname: DNS decides
    ("http://192.168.1.9:8080", ("10.0.0.0/8",)),        # outside the trusted range
    ("http://203.0.113.7:8080", ("0.0.0.0/0",)),         # public address
])
def test_plaintext_trust_is_limited_to_private_ip_literals(base_url, networks):
    gateway, calls = _recording_gateway(base_url, plaintext_networks=networks)
    with pytest.raises((Forbidden, InvalidInput)):
        gateway.generate(ModelRequest(prompt="q", tier="code"), _ctx(cloud=True))
    assert calls == []


def test_env_config_reads_the_plaintext_network_allowlist(monkeypatch):
    monkeypatch.setenv("SONDER_OPENAI_BASE_URL", "http://10.1.2.3:8080")
    monkeypatch.setenv("SONDER_OPENAI_ALLOW_HTTP_NETWORKS", "10.1.0.0/16; 192.168.0.0/16")
    cfg = gateway_module._config_from_env()
    assert cfg.plaintext_networks == ("10.1.0.0/16", "192.168.0.0/16")
