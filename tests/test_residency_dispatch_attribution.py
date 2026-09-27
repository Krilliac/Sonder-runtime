"""Residency attribution follows serialized requests, not planning calls."""
import io
import json
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters.inference.residency_feedback import ResidencyFeedback


@pytest.fixture
def wire(monkeypatch):
    sent, recorded = [], []
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(server, "_require_ollama_endpoint", lambda **_kwargs: None)
    monkeypatch.setattr(server, "_is_cloud_model_name", lambda model: model.endswith(":cloud"))
    monkeypatch.setattr(server, "_residency_feedback", lambda: SimpleNamespace(
        note_selection=lambda model, context: recorded.append((model, context)),
    ))
    monkeypatch.setattr(server, "dispatch_provider", lambda _provider, _path, _payload, send: send())

    def open_url(request, **_kwargs):
        sent.append(json.loads(request.data))
        return io.BytesIO(b'{"message":{"content":"ok"}}')

    monkeypatch.setattr(server, "OLLAMA_POOL", SimpleNamespace(enabled=False, open_url=open_url))
    return sent, recorded


def test_planning_only_context_does_not_claim_a_loaded_window(monkeypatch):
    feedback = ResidencyFeedback(lambda: pytest.fail("planning must not probe"), minimum_context=512)
    monkeypatch.setattr(server, "_residency_feedback", lambda: feedback)
    monkeypatch.setattr(server, "_is_cloud_model_name", lambda _model: False)
    monkeypatch.setattr(server, "_model_context_metadata", lambda _model: (262144, "27B"))
    monkeypatch.delenv("SONDER_CONTEXT_SIZE", raising=False)
    monkeypatch.delenv("SONDER_SESSION_NUM_CTX", raising=False)

    server._auto_model_context("qwen:27b")
    server._fanout_synthesis_prompt("{}", "qwen:27b")

    assert feedback._states == {}


def test_fanout_records_the_smaller_window_actually_dispatched(wire, monkeypatch):
    sent, recorded = wire
    monkeypatch.setattr(server, "_auto_model_context", lambda _model: 32768)
    monkeypatch.setattr(server, "_HOST_MODEL_REQUEST_ADMISSION", SimpleNamespace(try_acquire=lambda: None))

    assert server._fanout_synthesis_generate("qwen:27b", "{}") == "ok"

    assert len(sent) == 1
    assert 512 <= sent[0]["options"]["num_ctx"] < 32768
    assert recorded == [("qwen:27b", sent[0]["options"]["num_ctx"])]


def test_dispatched_context_comes_from_serialized_bytes(wire, monkeypatch):
    sent, recorded = wire
    payload = {"model": "qwen:27b", "options": {"num_ctx": 4096}}

    def dispatch(_provider, _path, metadata, send):
        metadata["options"]["num_ctx"] = 32768
        payload["options"]["num_ctx"] = 16384
        return send()

    monkeypatch.setattr(server, "dispatch_provider", dispatch)
    server._post("/api/chat", payload)
    assert sent[0]["options"]["num_ctx"] == 4096
    assert recorded == [("qwen:27b", 4096)]


@pytest.mark.parametrize("path,payload", [
    ("/api/show", {"model": "qwen:27b", "options": {"num_ctx": 4096}}),
    ("/api/generate", {"model": "qwen:27b", "keep_alive": "5m"}),
    ("/api/chat", {"model": "qwen:cloud", "options": {"num_ctx": 4096}}),
    ("/api/chat", {"model": "qwen:27b", "options": {"num_ctx": True}}),
])
def test_non_context_or_cloud_requests_do_not_supply_observations(wire, path, payload):
    _sent, recorded = wire
    server._post(path, payload)
    assert recorded == []


def test_rejected_provider_dispatch_is_not_recorded(wire, monkeypatch):
    sent, recorded = wire

    def reject(*_args):
        raise PermissionError("provider admission rejected")

    monkeypatch.setattr(server, "dispatch_provider", reject)
    with pytest.raises(PermissionError):
        server._post("/api/chat", {"model": "qwen:27b", "options": {"num_ctx": 4096}})
    assert sent == recorded == []


def test_dispatched_prewarm_invalidates_previous_window_attribution(wire, monkeypatch):
    calls = []
    def fetch():
        calls.append(1)
        return {"models": [{"name": "qwen:27b", "size": 20000, "size_vram": 10000}]}
    feedback = ResidencyFeedback(fetch, minimum_context=512)
    feedback.note_selection("qwen:27b", 16384)
    feedback.refresh("qwen:27b", geometry=None, kv_type="f16")
    assert feedback.ceiling("qwen:27b") is not None
    assert feedback.verdict("qwen:27b") is not None
    monkeypatch.setattr(server, "_residency_feedback", lambda: feedback)

    server._post("/api/generate", {"model": "qwen:27b", "keep_alive": "5m"})

    assert feedback.refresh("qwen:27b", geometry=None, kv_type="f16") is None
    assert feedback.ceiling("qwen:27b") is None
    assert feedback.verdict("qwen:27b") is None
    assert calls == [1]


def test_pool_routing_invalidates_single_origin_tracker(monkeypatch):
    primary = "http://127.0.0.1:11434"
    monkeypatch.setenv("SONDER_RESIDENCY_FEEDBACK", "1")
    monkeypatch.setattr(server, "BASE", primary)
    monkeypatch.setattr(server, "_RESIDENCY_FEEDBACK", None)
    pool = SimpleNamespace(configured_origins=(primary,))
    monkeypatch.setattr(server, "OLLAMA_POOL", pool)
    first = server._residency_feedback()
    first.note_selection("qwen:27b", 16384)

    pool.configured_origins = (primary, "http://127.0.0.1:11435")
    assert server._residency_feedback() is None
    pool.configured_origins = (primary,)
    assert server._residency_feedback() is not first


def test_inflight_probe_cannot_restore_an_invalidated_measurement():
    def fetch():
        feedback.forget_selection("qwen:27b")
        feedback.note_selection("qwen:27b", 4096)
        return {"models": [{"name": "qwen:27b", "size": 20000, "size_vram": 10000}]}

    feedback = ResidencyFeedback(fetch, minimum_context=512)
    feedback.note_selection("qwen:27b", 16384)

    assert feedback.refresh("qwen:27b", geometry=None, kv_type="f16") is None
    assert feedback.ceiling("qwen:27b") is None
    assert feedback.verdict("qwen:27b") is None
