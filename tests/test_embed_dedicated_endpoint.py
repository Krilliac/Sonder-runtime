"""SONDER_EMBED_BASE_URL sends embeddings to a dedicated Ollama host.

Motivation (2026-09-30): the workstation's 16 GB GPU holds one 27B chat model.
With OLLAMA_MAX_LOADED_MODELS=1 every embedding on the local primary evicts it
(the server log never shows more than one loaded runner), so the embedder
belongs on a LAN node. These tests pin the routing, the policy gate, the
circuit that stops a dead node from costing a full timeout per call, and the
opt-in local-CPU fallback.
"""
from __future__ import annotations

import io
import json
import urllib.error

import pytest

from sonder_runtime.adapters import embeddings

NODE = "https://10.77.0.2:8443"
LOCAL = "http://127.0.0.1:11434"


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self, *_):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _url(req):
    return req.full_url if hasattr(req, "full_url") else str(req)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    for name in ("SONDER_EMBED_BASE_URL", "SONDER_EMBED_FALLBACK",
                 "SONDER_EMBED_COOLDOWN_SECONDS", "SONDER_EMBED_ON_CPU",
                 "SONDER_EMBED_REVISION", "SONDER_EMBED_KEEP_ALIVE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OLLAMA_HOST", LOCAL)
    # Restored by monkeypatch after this fixture's teardown, so no test leaks a
    # remote BASE into the rest of the worker.
    monkeypatch.setattr(embeddings, "BASE", embeddings.BASE)
    monkeypatch.setattr(embeddings, "OLLAMA_HOST", embeddings.OLLAMA_HOST)
    monkeypatch.setattr(embeddings.embed_cache, "get", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(embeddings.embed_cache, "put", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(embeddings, "_accelerated_embed", lambda *a, **k: None)
    monkeypatch.setattr(embeddings, "_npu_prefer_active", lambda: False)
    monkeypatch.setattr(embeddings, "_npu_shadow_embed", lambda *a, **k: None)
    embeddings._close_circuit()
    yield
    embeddings._close_circuit()


def _opener(calls, *, node="ok"):
    """Fake transport: records embed calls as (origin, body); node = ok|down|502|404."""

    def fake_open(req, timeout=None, **_):
        url = _url(req)
        origin = url.split("/api/")[0]
        if url.endswith("/api/tags"):
            raise OSError("no revision probe in this test")
        if origin == NODE and node != "ok":
            if node == "down":
                raise urllib.error.URLError(ConnectionRefusedError("refused"))
            code = int(node)
            raise urllib.error.HTTPError(url, code, "err", {}, io.BytesIO(b"{}"))
        calls.append((origin, json.loads(req.data.decode())))
        return _Resp({"embedding": [0.25] * 768})

    return fake_open


def _dedicate(monkeypatch, fallback=None, cooldown=None):
    monkeypatch.setenv("SONDER_EMBED_BASE_URL", NODE)
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    if fallback is not None:
        monkeypatch.setenv("SONDER_EMBED_FALLBACK", fallback)
    if cooldown is not None:
        monkeypatch.setenv("SONDER_EMBED_COOLDOWN_SECONDS", str(cooldown))
    embeddings.configure_typed_endpoint(None)


def test_unset_keeps_embeddings_on_the_primary(monkeypatch):
    embeddings.configure_typed_endpoint(LOCAL)
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls))
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is not None
    assert [origin for origin, _ in calls] == [LOCAL]
    assert embeddings.embedding_route()["dedicated"] is False


def test_dedicated_endpoint_wins_over_the_typed_primary(monkeypatch):
    _dedicate(monkeypatch)
    embeddings.configure_typed_endpoint(LOCAL)  # composition root re-binds the primary
    assert embeddings.BASE == NODE
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls))
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is not None
    assert [origin for origin, _ in calls] == [NODE]
    assert "options" not in calls[0][1]


def test_remote_dedicated_endpoint_still_needs_explicit_consent(monkeypatch):
    _dedicate(monkeypatch)
    monkeypatch.delenv("SONDER_ALLOW_REMOTE_OLLAMA")
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls))
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is None
    assert calls == []


def test_plain_http_remote_is_refused_even_with_consent(monkeypatch):
    monkeypatch.setenv("SONDER_EMBED_BASE_URL", "http://10.77.0.2:11434")
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    embeddings.configure_typed_endpoint(None)
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls))
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is None
    assert calls == []


def test_explicit_base_bypasses_the_dedicated_route(monkeypatch):
    _dedicate(monkeypatch, fallback="local")
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls, node="down"))
    assert embeddings.embed("hi", base=LOCAL, model="nomic-embed-text:latest") is not None
    assert [origin for origin, _ in calls] == [LOCAL]
    assert embeddings.circuit_status()["open"] is False


@pytest.mark.parametrize("failure", ["down", "502", "503"])
def test_dead_node_without_fallback_opens_circuit_and_skips_the_network(monkeypatch, failure):
    _dedicate(monkeypatch)
    calls, attempts = [], []
    inner = _opener(calls, node=failure)

    def counting(req, timeout=None, **kw):
        if _url(req).endswith("/api/embeddings"):
            attempts.append(_url(req))
        return inner(req, timeout, **kw)

    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", counting)
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is None
    assert embeddings.circuit_status()["open"] is True
    assert embeddings.embed("again", model="nomic-embed-text:latest") is None
    assert len(attempts) == 1, "an open circuit must not dial the dead node again"
    assert embeddings._EMBED_STATE.fallback_reason == "embed_endpoint_unavailable"


def test_missing_model_is_not_an_outage(monkeypatch):
    _dedicate(monkeypatch, fallback="local")
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls, node="404"))
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is None
    assert embeddings.circuit_status()["open"] is False
    assert calls == [], "a 4xx is a configuration error, not a reason to fall back"


def test_local_fallback_forces_cpu_and_reports_itself(monkeypatch):
    _dedicate(monkeypatch, fallback="local")
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls, node="down"))
    result = embeddings.embed_result("hi", model="nomic-embed-text:latest")
    assert result is not None
    assert [origin for origin, _ in calls] == [LOCAL]
    assert calls[0][1]["options"] == {"num_gpu": 0}
    assert result["fallback_reason"] == "embed_endpoint_unavailable_local_cpu"
    assert result["provider"] == "ollama"


def test_circuit_recovers_after_cooldown(monkeypatch):
    _dedicate(monkeypatch, cooldown=0)
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls, node="down"))
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is None
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls))
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is not None
    assert [origin for origin, _ in calls] == [NODE]
    assert embeddings.circuit_status() == {
        "open": False, "retry_in_seconds": 0.0, "consecutive_failures": 0,
    }


def test_fallback_is_never_a_second_remote(monkeypatch):
    _dedicate(monkeypatch, fallback="local")
    monkeypatch.setenv("OLLAMA_HOST", "https://10.77.0.9:8443")
    assert embeddings.embedding_route()["fallback"] is None


@pytest.mark.parametrize("value, sent", [
    (None, "absent"), ("24h", "24h"), ("-1", -1), ("300", 300), ("forever", "absent"),
])
def test_keep_alive_is_opt_in_and_validated(monkeypatch, value, sent):
    _dedicate(monkeypatch)
    if value is not None:
        monkeypatch.setenv("SONDER_EMBED_KEEP_ALIVE", value)
    calls = []
    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", _opener(calls))
    assert embeddings.embed("hi", model="nomic-embed-text:latest") is not None
    body = calls[-1][1]
    assert body.get("keep_alive", "absent") == sent


@pytest.mark.parametrize("value, expected", [("", "none"), ("LOCAL", "local"), ("cloud", "none")])
def test_fallback_mode_is_validated(monkeypatch, value, expected):
    monkeypatch.setenv("SONDER_EMBED_FALLBACK", value)
    assert embeddings.fallback_mode() == expected


# --- sonder doctor: the embedder must exist where embeddings are sent ---------

def _doctor(monkeypatch, tags_by_origin):
    import sonder_doctor
    from types import SimpleNamespace

    def fake_open(req, timeout=None, **_):
        origin = _url(req).split("/api/")[0]
        if origin not in tags_by_origin:
            raise OSError("connection refused")
        return _Resp({"models": [{"name": n} for n in tags_by_origin[origin]]})

    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", fake_open)
    monkeypatch.setattr(embeddings, "EMBED_MODEL", "nomic-embed-text")
    config = SimpleNamespace(ollama=SimpleNamespace(url=LOCAL),
                             membership=SimpleNamespace(mode="static"))
    return sonder_doctor._check_embeddings(config=config)


def test_doctor_warns_when_the_embedder_is_not_installed_where_it_is_sent(monkeypatch):
    # The live-stack failure this check exists for: model pulled on Node1,
    # embeddings still sent to the primary, every recall silently lexical.
    result = _doctor(monkeypatch, {LOCAL: ["qwen3.8:27b"]})
    assert result["status"] == "warn"
    assert "lacks nomic-embed-text:latest" in result["detail"]
    assert "(primary)" in result["detail"]


def test_doctor_reports_dedicated_endpoint_and_ready_fallback(monkeypatch):
    _dedicate(monkeypatch, fallback="local")
    result = _doctor(monkeypatch, {NODE: ["nomic-embed-text:latest"],
                                   LOCAL: ["nomic-embed-text"]})
    assert result["status"] == "ok"
    assert "10.77.0.2 (dedicated)" in result["detail"]
    assert "local-CPU fallback ready" in result["detail"]


def test_doctor_warns_when_dedicated_endpoint_is_down(monkeypatch):
    _dedicate(monkeypatch)
    result = _doctor(monkeypatch, {LOCAL: ["nomic-embed-text:latest"]})
    assert result["status"] == "warn"
    assert "unreachable" in result["detail"]


def test_doctor_warns_when_fallback_lacks_the_model(monkeypatch):
    _dedicate(monkeypatch, fallback="local")
    result = _doctor(monkeypatch, {NODE: ["nomic-embed-text:latest"], LOCAL: []})
    assert result["status"] == "warn"
    assert "fallback" in result["detail"]
