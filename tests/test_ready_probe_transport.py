"""The /ready Ollama probe uses the same transport policy as real Ollama calls.

Regression coverage for a defect: ``RuntimeLifecycle.probe_ollama_once`` went
through ``urllib.request.urlopen``. That ignored the configured Ollama CA
bundle (a private-CA TLS worker failed /ready while inference worked) and
honoured ``HTTP(S)_PROXY`` (a local health probe could leave the machine).
It also took its target from ``OLLAMA_HOST`` alone, so with the worker bound
as the typed endpoint (the ``serve`` path, which does not export
``OLLAMA_HOST``) it probed the default local port instead of the worker.
"""
from __future__ import annotations

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sonder_runtime.adapters.inference import ollama_endpoint
from sonder_runtime.adapters.web import lifecycle as sonder_lifecycle
from sonder_runtime.platform.service_state import DependencyState

_PROXY_VARIABLES = (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "http_proxy", "https_proxy", "all_proxy", "no_proxy",
)


def _recording_server(seen: list[str]) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append(self.path)
            body = b'{"models": []}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.fixture()
def runtime_lifecycle(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("SONDER_HOME", str(home))
    monkeypatch.setenv("SONDER_OPERATIONS_DB", str(home / "operations.db"))
    for name in _PROXY_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    # No typed endpoint or CA bundle left bound by an earlier test; each test
    # binds what it needs and monkeypatch restores the previous values.
    monkeypatch.setattr(ollama_endpoint, "_configured_endpoint", None)
    monkeypatch.setattr(ollama_endpoint, "_configured_ca_bundle", None)
    sonder_lifecycle.reset_for_tests()
    yield sonder_lifecycle.get()
    sonder_lifecycle.reset_for_tests()


def _ollama_state(instance) -> DependencyState:
    return next(
        dep.state for dep in instance.tracker.snapshot().dependencies
        if dep.name == "ollama"
    )


def test_ready_probe_ignores_environment_proxies(runtime_lifecycle, monkeypatch):
    ollama_seen: list[str] = []
    proxy_seen: list[str] = []
    ollama = _recording_server(ollama_seen)
    proxy = _recording_server(proxy_seen)
    try:
        monkeypatch.setenv(
            "OLLAMA_HOST", f"http://127.0.0.1:{ollama.server_address[1]}"
        )
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("http_proxy", proxy_url)
        # urlopen() caches a process-wide opener that froze the proxy
        # environment when it was built; drop it so a probe that still used
        # urlopen() would really pick the proxy up.
        monkeypatch.setattr(urllib.request, "_opener", None)

        assert runtime_lifecycle.probe_ollama_once(timeout=5) is True
    finally:
        for server in (ollama, proxy):
            server.shutdown()
            server.server_close()

    assert proxy_seen == []
    assert ollama_seen == ["/api/tags"]
    assert _ollama_state(runtime_lifecycle) is DependencyState.READY


@pytest.mark.parametrize("environment_host", [None, "decoy"])
def test_ready_probe_targets_the_typed_endpoint_not_ollama_host(
    runtime_lifecycle, monkeypatch, environment_host,
):
    typed_seen: list[str] = []
    decoy_seen: list[str] = []
    bypassed: list[str] = []
    typed = _recording_server(typed_seen)
    decoy = _recording_server(decoy_seen)

    def plain_urlopen(request, *_args, **_kwargs):
        # A probe that leaves the Ollama transport is recorded and fails; it
        # never reaches whatever listens on the default local Ollama port.
        bypassed.append(getattr(request, "full_url", str(request)))
        raise OSError("probe bypassed the Ollama transport")

    try:
        typed_origin = f"http://127.0.0.1:{typed.server_address[1]}"
        if environment_host is None:
            monkeypatch.delenv("OLLAMA_HOST", raising=False)
        else:
            monkeypatch.setenv(
                "OLLAMA_HOST", f"http://127.0.0.1:{decoy.server_address[1]}"
            )
        monkeypatch.setattr(urllib.request, "urlopen", plain_urlopen)
        ollama_endpoint.configure_typed_endpoint(typed_origin)

        assert ollama_endpoint.configured_origin() == typed_origin
        healthy = runtime_lifecycle.probe_ollama_once(timeout=5)
    finally:
        for server in (typed, decoy):
            server.shutdown()
            server.server_close()

    assert bypassed == []
    assert decoy_seen == []
    assert typed_seen == ["/api/tags"]
    assert healthy is True
    assert _ollama_state(runtime_lifecycle) is DependencyState.READY


def test_ready_probe_reports_unavailable_when_the_endpoint_is_refused(
    runtime_lifecycle, monkeypatch,
):
    # A remote endpoint without the remote opt-in is refused by the shared
    # resolver; the probe reports that as a failed probe, it does not raise.
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv(ollama_endpoint.REMOTE_OPT_IN, raising=False)
    ollama_endpoint.configure_typed_endpoint("https://ollama.example.test:11434")
    with pytest.raises(ValueError):
        ollama_endpoint.configured_origin()

    assert runtime_lifecycle.probe_ollama_once(timeout=2) is False
    assert _ollama_state(runtime_lifecycle) is DependencyState.UNAVAILABLE


def test_ready_probe_verifies_https_with_the_configured_ca_bundle(
    runtime_lifecycle, monkeypatch, tmp_path,
):
    bundle = tmp_path / "ollama-ca.pem"
    bundle.write_text("placeholder", encoding="ascii")
    origin = "https://ollama.example.test:11434"
    monkeypatch.setenv("OLLAMA_HOST", origin)
    monkeypatch.setenv(ollama_endpoint.REMOTE_OPT_IN, "1")
    monkeypatch.delenv("SONDER_OLLAMA_CA_BUNDLE", raising=False)

    contexts: list[dict] = []
    handlers_seen: list[tuple] = []
    opened: list[tuple[str, float]] = []
    bypassed: list[str] = []
    sentinel = object()

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    class Opener:
        def open(self, request, timeout):
            opened.append((request.full_url, timeout))
            return Response()

    def build_opener(*handlers):
        handlers_seen.append(handlers)
        return Opener()

    def plain_urlopen(request, *_args, **_kwargs):
        # No network: a probe that bypasses the Ollama transport is recorded
        # and fails instead of resolving the placeholder host.
        bypassed.append(getattr(request, "full_url", str(request)))
        raise OSError("probe bypassed the Ollama transport")

    monkeypatch.setattr(
        ollama_endpoint.ssl, "create_default_context",
        lambda **kwargs: contexts.append(kwargs) or sentinel,
    )
    monkeypatch.setattr(urllib.request, "build_opener", build_opener)
    monkeypatch.setattr(urllib.request, "urlopen", plain_urlopen)
    ollama_endpoint.configure_typed_ca_bundle(str(bundle))
    try:
        healthy = runtime_lifecycle.probe_ollama_once(timeout=2)
    finally:
        ollama_endpoint.configure_typed_ca_bundle(None)

    assert bypassed == []
    assert healthy is True
    # Only the configured bundle anchors trust for the probe.
    assert contexts == [{"cafile": str(bundle)}]
    assert opened == [(origin + "/api/tags", 2)]
    # The probe's opener carries the no-proxy handler and the bundle context.
    (handlers,) = handlers_seen
    assert ollama_endpoint._PROXY_HANDLER in handlers
    https = [h for h in handlers if isinstance(h, urllib.request.HTTPSHandler)]
    assert len(https) == 1 and https[0]._context is sentinel
    assert _ollama_state(runtime_lifecycle) is DependencyState.READY
