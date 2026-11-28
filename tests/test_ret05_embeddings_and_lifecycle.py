"""RET-05 Subsystem 2 & 3 tests: SI embeddings, readiness, preflight, doctor, and headless."""
from __future__ import annotations

import http.server
import json
import threading

from sonder_runtime.adapters import embeddings
from sonder_runtime.adapters.preflight import run_preflight
from sonder_runtime.adapters.web.lifecycle import RuntimeLifecycle
from sonder_runtime.application.observability.ecosystem_status import ecosystem_warnings
from sonder_runtime.platform.config import SonderConfig, InferenceConfig


def _start_fake_si(handlers):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            code, payload = handlers.get(("GET", self.path), (404, {"error": {"message": "not found"}}))
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8"))
            fn = handlers.get(("POST", self.path))
            if callable(fn):
                code, payload = fn(req, dict(self.headers))
            else:
                code, payload = (404, {"error": {"message": "not found"}})
            body = json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_embeddings_use_si_v1_embeddings_and_identity(monkeypatch):
    digest = "a" * 64
    seen = []

    def handle_embed(req, headers):
        seen.append((req, headers))
        vec = [0.25] * 4
        return 200, {
            "object": "list",
            "model": req["model"],
            "data": [{"object": "embedding", "index": 0, "embedding": vec}],
        }

    srv, base = _start_fake_si({
        ("GET", "/v1/sonder/identity?model=nomic-embed-text%3Alatest"): (200, {
            "schema": "sonder.inference.identity/1",
            "backend_identity": {"model_digest": digest},
        }),
        ("GET", "/v1/sonder/identity?model=nomic-embed-text"): (200, {
            "schema": "sonder.inference.identity/1",
            "backend_identity": {"model_digest": digest},
        }),
        ("POST", "/v1/embeddings"): handle_embed,
    })
    try:
        monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", base)
        monkeypatch.setenv("SONDER_EMBED_DIM", "4")
        monkeypatch.delenv("SONDER_EMBED_REVISION", raising=False)
        res = embeddings.embed_result("hello si", base=base, model="nomic-embed-text")
        assert res is not None
        assert res["vector"] == [0.25] * 4
        assert res["revision"] == f"si-model-sha256:{digest}"
        assert res["provider"] == "sonder_inference"
        assert len(seen) == 1
        assert seen[0][0]["input"] == "hello si" or seen[0][0]["input"] == ["hello si"]
    finally:
        srv.shutdown()
        srv.server_close()


def test_embeddings_soft_fail_when_si_unreachable(monkeypatch):
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", "http://127.0.0.1:1")
    assert embeddings.embed("unreachable", timeout=0.5, base="http://127.0.0.1:1") is None


def test_ecosystem_status_has_no_stale_ollama_embedding_warning():
    warnings = ecosystem_warnings(
        projection={
            "default_generation_provider": "sonder_inference",
            "tier_providers": {"code": "sonder_inference"},
            "embedding_provider": "sonder_inference",
        },
        statuses={},
        export_enabled=True,
        observatory_origins=["http://127.0.0.1:4173"],
    )
    assert not any("SONDER_EMBEDDING_PROVIDER=ollama" in w for w in warnings)


def test_lifecycle_readiness_probes_si_health(monkeypatch):
    srv, base = _start_fake_si({
        ("GET", "/v1/sonder/health"): (200, {
            "api_version": 1,
            "state": "ready",
            "backend": "llamacpp",
            "models": [{"id": "qwen3.6:35b", "state": "resident"}],
        }),
    })
    try:
        monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", base)
        lc = RuntimeLifecycle()
        assert lc.probe_inference_once(timeout=2.0) is True
        snap = lc.tracker.snapshot()
        deps = {d.name: d for d in snap.dependencies}
        assert "inference" in deps
        assert deps["inference"].state.value == "ready"
    finally:
        srv.shutdown()
        srv.server_close()


def test_lifecycle_readiness_fails_when_si_starting(monkeypatch):
    srv, base = _start_fake_si({
        ("GET", "/v1/sonder/health"): (503, {"api_version": 1, "state": "starting"}),
    })
    try:
        monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", base)
        lc = RuntimeLifecycle()
        assert lc.probe_inference_once(timeout=2.0) is False
        snap = lc.tracker.snapshot()
        deps = {d.name: d for d in snap.dependencies}
        assert deps["inference"].state.value == "unavailable"
    finally:
        srv.shutdown()
        srv.server_close()


def test_preflight_requires_si_health_not_ollama(monkeypatch, tmp_path):
    srv, base = _start_fake_si({
        ("GET", "/v1/sonder/health"): (200, {
            "api_version": 1,
            "state": "ready",
            "backend": "llamacpp",
            "models": [{"id": "qwen3.6:35b", "state": "resident"}],
        }),
    })
    try:
        monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", base)
        cfg = SonderConfig(
            state=SonderConfig().state.__class__(home=str(tmp_path / "home")),
            inference=InferenceConfig(url=base),
        )
        report = run_preflight(cfg, check_ollama=True, ollama_timeout=2.0)
        names = {c.name: c for c in report.checks}
        assert "inference" in names or "sonder_inference" in names
        assert "ollama" not in names
        inf = names.get("inference") or names["sonder_inference"]
        assert inf.ok is True
    finally:
        srv.shutdown()
        srv.server_close()


def test_headless_never_spawns_ollama_and_probes_si(monkeypatch):
    import sonder_headless

    spawned = []
    monkeypatch.setattr(sonder_headless, "_popen", lambda *a, **k: spawned.append(a) or 999)
    srv, base = _start_fake_si({
        ("GET", "/v1/sonder/health"): (200, {
            "api_version": 1,
            "state": "ready",
            "backend": "llamacpp",
            "models": [{"id": "sonder:latest", "state": "resident"}],
        }),
        ("GET", "/v1/models"): (200, {
            "object": "list",
            "data": [{"id": "sonder:latest"}],
        }),
    })
    try:
        monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", base)
        msg = sonder_headless.start_ollama()
        assert spawned == []
        assert "sonder-infer" in msg.lower() or "inference" in msg.lower()
        ok, alias_msg = sonder_headless.ensure_sonder_alias()
        assert ok is True
        assert spawned == []
    finally:
        srv.shutdown()
        srv.server_close()
