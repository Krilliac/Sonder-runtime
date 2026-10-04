"""Ordered real-composition cases selected only by the fresh serial driver.

This fixture module deliberately lives outside default test-file patterns.
The outer regression selects all three cases in one child and verifies their
identities and results; a consumer cannot qualify without its producer in
the same process.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import ssl
import threading

import pytest

from sonder_runtime.adapters.inference import ollama_endpoint, ollama_pool
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.platform.config import OllamaConfig, SonderConfig, StateConfig


_PRODUCER_COMPLETED = False


@pytest.fixture(autouse=True)
def _require_serial_order(request):
    if hasattr(request.config, "workerinput"):
        pytest.fail("This cross-test proof requires one serial pytest process")


@pytest.fixture(scope="module")
def local_certificates(tmp_path_factory):
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.x509.oid import NameOID

    root = tmp_path_factory.mktemp("ca-pool-isolation")
    certificates = {}
    for label in ("typed-a", "environment-b"):
        key = Ed25519PrivateKey.generate()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1))
            .not_valid_after(now + timedelta(hours=1))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([
                x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
            ]), critical=False)
            .sign(key, algorithm=None)
        )
        cert_path = root / (label + ".pem")
        key_path = root / (label + ".key")
        cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        certificates[label] = cert_path, key_path
    return certificates


def test_01_typed_composition_owns_ca_and_pool(local_certificates, tmp_path):
    """Create and close real A owners; per-test fixtures must retire bindings."""
    global _PRODUCER_COMPLETED
    # A fresh serial process should begin without typed application bindings.
    # Fail explicitly if collection/import changed that prerequisite; do not
    # reset state here and mask what the suite-wide fixture actually restores.
    assert bootstrap_app.built_default_app() is None
    with ollama_endpoint._configuration_lock:
        assert ollama_endpoint._configured_ca_bundle is None
    with ollama_pool._configuration_lock:
        assert ollama_pool._configured_pool is None
        assert ollama_pool._configured_workers is None
    config = SonderConfig(
        state=StateConfig(home=str(tmp_path / "state")),
        ollama=OllamaConfig(
            url="http://127.0.0.1:18443",
            workers=("http://127.0.0.2:18443",),
            allow_remote=False,
            trusted_origins=(),
            ca_bundle=str(local_certificates["typed-a"][0]),
            worker_failure_threshold=2,
            worker_cooldown_seconds=2,
            worker_admission_timeout_ms=200,
            worker_capability_ttl_seconds=20,
            worker_probe_timeout_ms=200,
            worker_max_inflight=2,
            worker_queue_depth=3,
            worker_pool_max_workers=4,
            worker_capability_probe_parallelism=2,
            worker_capability_probe_batch_size=3,
            worker_status_page_size=3,
        ),
    )
    application = bootstrap_app.build_application(config=config)
    try:
        assert ollama_endpoint.tls_trust_source() == "configured-ca-bundle"
        assert ollama_pool.from_environment(config.ollama.url) is application.inference_pool
        assert application.inference_pool.configured_origins == (
            config.ollama.url, *config.ollama.workers,
        )
    finally:
        application.close_providers(timeout=2)
    _PRODUCER_COMPLETED = True


def test_02_next_test_verifies_local_tls_with_its_environment_bundle(
    local_certificates, monkeypatch,
):
    assert _PRODUCER_COMPLETED, "Run the producing case before this consumer"
    certificate, key = local_certificates["environment-b"]
    monkeypatch.setenv("SONDER_OLLAMA_CA_BUNDLE", str(certificate))
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(str(certificate), str(key))

    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            self.request.settimeout(2)
            super().setup()

        def do_GET(self):
            assert self.path == "/api/version"
            body = b'{"version":"isolation-b"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    class LocalTLS(ThreadingHTTPServer):
        daemon_threads = False
        block_on_close = True

        def get_request(self):
            raw, address = super().get_request()
            raw.settimeout(2)
            try:
                return context.wrap_socket(raw, server_side=True), address
            except BaseException:
                raw.close()
                raise

    listener = LocalTLS(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=listener.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        url = "https://127.0.0.1:%d/api/version" % listener.server_address[1]
        with ollama_endpoint.open_url(url, timeout=2) as response:
            assert response.read() == b'{"version":"isolation-b"}'
    finally:
        listener.shutdown()
        listener.server_close()
        thread.join(3)
        assert not thread.is_alive(), "The local TLS listener outlived its contract"


def test_03_next_test_builds_a_pool_from_its_environment(monkeypatch):
    assert _PRODUCER_COMPLETED, "Run the producing case before this consumer"
    primary = "http://127.0.0.3:18443"
    worker = "http://127.0.0.4:18443"
    monkeypatch.setenv("OLLAMA_HOST", primary)
    monkeypatch.setenv("SONDER_OLLAMA_WORKERS", worker)
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "0")
    monkeypatch.setenv("SONDER_TRUSTED_ORIGINS", "")
    monkeypatch.setenv("SONDER_OLLAMA_WORKER_PROBE_PARALLELISM", "1")
    monkeypatch.setenv("SONDER_OLLAMA_WORKER_PROBE_BATCH_SIZE", "2")
    monkeypatch.setenv("SONDER_OLLAMA_WORKER_STATUS_PAGE_SIZE", "2")
    # Passing an explicit environment dict would bypass the exact leaking seam.
    pool = ollama_pool.from_environment(primary)
    try:
        assert pool.configured_origins == (primary, worker)
        page = pool.status()
        assert page["probe_parallelism"] == 1
        assert page["probe_batch_size"] == 2
        assert page["status_page_size"] == 2
    finally:
        assert pool.drain(timeout_seconds=2)
