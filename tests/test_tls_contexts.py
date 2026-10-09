"""Outbound HTTPS adapters build their TLS context once, never per request.

Loading trust anchors costs ~150-250 ms on Windows.  Every opener site below
used to pay it on every request (``build_opener()`` builds a default HTTPS
context eagerly, even for plain-HTTP URLs), which made each small loopback
request take ~170 ms instead of ~15 ms.
"""
from __future__ import annotations

import json
import os
import ssl
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import certifi
import pytest

from sonder_runtime.adapters import client_transport, tls_contexts
from sonder_runtime.adapters.cluster import http_control_state
from sonder_runtime.adapters.compute_fabric import http_client as compute_http
from sonder_runtime.adapters.inference import ollama_endpoint
from sonder_runtime.adapters.memory_replication import http_client as replication_http


@pytest.fixture
def trust_loads(monkeypatch):
    """Count every trust-anchor load from here on."""
    loads: list[str] = []
    for name in ("load_default_certs", "load_verify_locations"):
        real = getattr(ssl.SSLContext, name)

        def counting(self, *args, _real=real, _name=name, **kwargs):
            loads.append(_name)
            return _real(self, *args, **kwargs)

        monkeypatch.setattr(ssl.SSLContext, name, counting)
    return loads


@pytest.fixture
def loopback_url():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            body = json.dumps({"ok": True}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/ping" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


OPENERS = {
    "compute_fabric": lambda url: compute_http._default_opener(urllib.request.Request(url), timeout=5),
    "memory_replication": lambda url: replication_http._default_opener(urllib.request.Request(url), timeout=5),
    "cluster": lambda url: http_control_state._default_opener(urllib.request.Request(url), timeout=5),
    "client_transport": lambda url: client_transport._open(urllib.request.Request(url)),
}


@pytest.mark.parametrize("site", sorted(OPENERS))
def test_per_call_openers_never_reload_trust_anchors(site, loopback_url, monkeypatch):
    open_once = OPENERS[site]
    with open_once(loopback_url) as response:  # warm: one load per process
        assert json.loads(response.read()) == {"ok": True}
    loads: list[str] = []
    real = ssl.SSLContext.load_default_certs
    monkeypatch.setattr(ssl.SSLContext, "load_default_certs",
                        lambda self, *a, **k: (loads.append(1), real(self, *a, **k))[1])
    for _ in range(3):
        with open_once(loopback_url) as response:
            response.read()
    assert loads == [], "%s reloaded the trust store %d times in 3 requests" % (site, len(loads))


def test_pinned_mobility_client_reuses_one_verifying_context(trust_loads):
    seen = []

    class Refuse(Exception):
        pass

    def factory(host, port, timeout, context):
        seen.append(context)
        raise Refuse

    client = compute_http.PinnedHttpsClient(
        "https://node.example:8443", "0" * 64, timeout_seconds=5, connection_factory=factory,
    )
    for _ in range(4):
        with pytest.raises(compute_http.PinnedHttpsClientError):
            client.request("GET", "/v1/status", body=None, headers_supplier=dict, response_limit=1024)
    assert len(seen) == 4 and all(context is seen[0] for context in seen)
    assert seen[0].verify_mode == ssl.CERT_REQUIRED and seen[0].check_hostname
    assert trust_loads.count("load_default_certs") <= 1


def test_ollama_ca_bundle_context_is_reused_rotated_and_bundle_only(tmp_path, monkeypatch, trust_loads):
    bundle = tmp_path / "ollama-ca.pem"
    bundle.write_bytes(open(certifi.where(), "rb").read())
    monkeypatch.setattr(ollama_endpoint, "_ca_bundle", lambda: str(bundle))
    monkeypatch.setattr(ollama_endpoint, "configured_origin",
                        lambda *_a, **_k: "https://ollama.example:11434")
    contexts = []

    def capture(self, request, timeout=None):
        handler = next(h for h in self.handlers if isinstance(h, urllib.request.HTTPSHandler))
        contexts.append(handler._context)
        return "sent"

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", capture)
    for _ in range(4):
        assert ollama_endpoint.open_url("https://ollama.example:11434/api/tags") == "sent"
    assert all(context is contexts[0] for context in contexts)
    assert trust_loads == ["load_verify_locations"], trust_loads

    # Trusts exactly the bundle, with verification on.
    context = contexts[0]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    expected = ssl.create_default_context(cafile=str(bundle)).get_ca_certs()
    assert len(context.get_ca_certs()) == len(expected) > 0

    # A rotated bundle is picked up on the next request.
    stamp = bundle.stat().st_mtime_ns + 1_000_000_000
    os.utime(bundle, ns=(stamp, stamp))
    ollama_endpoint.open_url("https://ollama.example:11434/api/tags")
    assert contexts[-1] is not context


def test_default_context_verifies_and_follows_trust_path_variables(tmp_path, monkeypatch):
    first = tls_contexts.default_https_context()
    assert first.verify_mode == ssl.CERT_REQUIRED and first.check_hostname
    assert tls_contexts.default_https_context() is first
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "bundle.pem"))
    assert tls_contexts.default_https_context() is not first
