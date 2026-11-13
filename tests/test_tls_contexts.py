"""Outbound HTTPS adapters build their TLS context once, never per request.

Loading trust anchors costs ~150-250 ms on Windows.  Every opener site below
used to pay it on every request (``build_opener()`` builds a default HTTPS
context eagerly, even for plain-HTTP URLs), which made each small loopback
request take ~170 ms instead of ~15 ms.
"""
from __future__ import annotations

import datetime
import json
import os
import ssl
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

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


def _ca_pem(common_name: str) -> bytes:
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=30))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM)


def _same_length_ca_pems() -> tuple[bytes, bytes]:
    """Two distinct one-CA PEMs of identical byte length (trailing newlines pad)."""
    first, second = _ca_pem("sonder-test-ca-A"), _ca_pem("sonder-test-ca-B")
    size = max(len(first), len(second))
    first, second = first.ljust(size, b"\n"), second.ljust(size, b"\n")
    assert len(first) == len(second) and first != second
    return first, second


def _subjects(context: ssl.SSLContext) -> set[str]:
    return {
        dict(pair[0] for pair in cert["subject"]).get("commonName", "")
        for cert in context.get_ca_certs()
    }


@pytest.fixture(autouse=True)
def _fresh_shared_contexts():
    def clear():
        reset = getattr(tls_contexts, "reset_https_contexts", None)
        if reset is not None:
            reset()
        for name in ("_default", "_bundle"):
            cached = getattr(tls_contexts, name, None)
            if hasattr(cached, "cache_clear"):
                cached.cache_clear()

    clear()
    yield
    clear()


def test_bundle_context_is_built_per_call_and_tracks_rotation(tmp_path, monkeypatch, trust_loads):
    pem_a, pem_b = _same_length_ca_pems()
    bundle = tmp_path / "ollama-ca.pem"
    bundle.write_bytes(pem_a)
    monkeypatch.setattr(ollama_endpoint, "_ca_bundle", lambda: str(bundle))
    monkeypatch.setattr(ollama_endpoint, "configured_origin",
                        lambda *_a, **_k: "https://ollama.example:11434")
    contexts = []

    def capture(self, request, timeout=None):
        handler = next(h for h in self.handlers if isinstance(h, urllib.request.HTTPSHandler))
        contexts.append(handler._context)
        return "sent"

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", capture)
    for _ in range(3):
        assert ollama_endpoint.open_url("https://ollama.example:11434/api/tags") == "sent"
    # Pinned-bundle contexts are built per call (as on main), never shared.
    assert len({id(context) for context in contexts}) == 3
    assert trust_loads == ["load_verify_locations"] * 3, trust_loads

    # Trusts exactly the bundle, with verification on.
    context = contexts[0]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert _subjects(context) == {"sonder-test-ca-A"}

    # Same-size replacement with the mtime preserved (cp -p / rsync -a).
    before = bundle.stat().st_mtime_ns
    bundle.write_bytes(pem_b)
    os.utime(bundle, ns=(before, before))
    assert bundle.stat().st_size == len(pem_a) and bundle.stat().st_mtime_ns == before
    ollama_endpoint.open_url("https://ollama.example:11434/api/tags")
    assert _subjects(contexts[-1]) == {"sonder-test-ca-B"}


def test_removed_bundle_errors_not_system_fallback(tmp_path, monkeypatch):
    missing = tmp_path / "gone.pem"
    with pytest.raises(FileNotFoundError):
        tls_contexts.bundle_https_context(str(missing))

    # The Ollama open path with a configured bundle that no longer exists.
    bundle = tmp_path / "ollama-ca.pem"
    bundle.write_bytes(_ca_pem("sonder-test-ca-A"))
    monkeypatch.setattr(ollama_endpoint, "_configured_ca_bundle", str(bundle))
    monkeypatch.setattr(ollama_endpoint, "configured_origin",
                        lambda *_a, **_k: "https://ollama.example:11434")
    opened = []
    monkeypatch.setattr(urllib.request.OpenerDirector, "open",
                        lambda self, *a, **k: opened.append(1) or "sent")
    bundle.unlink()
    with pytest.raises(ValueError):
        ollama_endpoint.open_url("https://ollama.example:11434/api/tags")
    assert opened == []


def test_default_context_rebuilds_when_ssl_cert_file_content_changes(tmp_path, monkeypatch):
    pem_a, pem_b = _ca_pem("sonder-test-ca-A"), _ca_pem("sonder-test-ca-B")
    cert_file = tmp_path / "trust.pem"
    cert_file.write_bytes(pem_a)
    monkeypatch.setenv("SSL_CERT_FILE", str(cert_file))
    first = tls_contexts.default_https_context()
    assert "sonder-test-ca-A" in _subjects(first)
    assert tls_contexts.default_https_context() is first

    cert_file.write_bytes(pem_b)
    stamp = cert_file.stat().st_mtime_ns + 5_000_000_000
    os.utime(cert_file, ns=(stamp, stamp))
    second = tls_contexts.default_https_context()
    assert second is not first
    subjects = _subjects(second)
    assert "sonder-test-ca-B" in subjects and "sonder-test-ca-A" not in subjects


def test_default_context_refreshes_after_max_age(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(tls_contexts.time, "monotonic", lambda: now[0])
    first = tls_contexts.default_https_context()
    now[0] += tls_contexts._MAX_AGE_SECONDS - 1
    assert tls_contexts.default_https_context() is first
    now[0] += 2
    assert tls_contexts.default_https_context() is not first


def test_every_site_verifies(tmp_path):
    bundle = tmp_path / "ca.pem"
    bundle.write_bytes(_ca_pem("sonder-test-ca-A"))
    handler_context = tls_contexts.https_handler()._context
    for context in (tls_contexts.default_https_context(),
                    tls_contexts.bundle_https_context(str(bundle)),
                    handler_context):
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True


def test_default_context_verifies_and_follows_trust_path_variables(tmp_path, monkeypatch):
    first = tls_contexts.default_https_context()
    assert first.verify_mode == ssl.CERT_REQUIRED and first.check_hostname
    assert tls_contexts.default_https_context() is first
    monkeypatch.setenv("SSL_CERT_FILE", str(tmp_path / "bundle.pem"))
    assert tls_contexts.default_https_context() is not first
