"""Focused tests for the fail-soft semantic embedding adapter."""

import threading
import time

import pytest

import sonder_runtime.adapters.semantic_tier as adapter
from sonder_runtime.domain.routing import semantic_tier


def _bank(monkeypatch):
    monkeypatch.setattr(
        semantic_tier,
        "EXAMPLE_BANK",
        {
            "fast": ("fast alpha", "fast beta"),
            "general": ("general alpha", "general beta"),
            "code": ("code alpha", "code beta"),
            "reasoning": ("reason alpha", "reason beta"),
            "vision": ("vision alpha", "vision beta"),
        },
    )
    monkeypatch.setattr(semantic_tier, "BANK_DIGEST", "test-bank")
    # These exercise the centroid path: no trained head.
    monkeypatch.setenv("SONDER_SEMANTIC_TIER_HEAD", "Z:/no/such/head.json")
    with adapter._cache_lock:
        adapter._cache.clear()
        adapter._flight = None


def test_fake_embedder_classifies_and_is_cached(monkeypatch):
    _bank(monkeypatch)
    calls = []

    def fake(text):
        calls.append(text)
        if "code" in text:
            return [1.0, 0.0, 0.0, 0.0]
        if "reason" in text:
            return [0.0, 1.0, 0.0, 0.0]
        if "vision" in text:
            return [0.0, 0.0, 1.0, 0.0]
        return [0.0, 0.0, 0.0, 1.0]

    result = adapter.semantic_signal("code query", embedder=fake, model="bge-m3")
    assert result and result["tier"] == "code"
    assert result["model"] == "bge-m3:latest"
    first_count = len(calls)
    assert adapter.semantic_signal("code query", embedder=fake, model="bge-m3")
    assert len(calls) == first_count + 1


def test_vision_is_never_returned(monkeypatch):
    _bank(monkeypatch)

    def fake(text):
        return [0.0, 0.0, 1.0, 0.0] if "vision" in text else [0.0, 0.0, 0.0, 1.0]

    assert adapter.semantic_signal("vision query", embedder=fake, model="bge-m3") is None


def test_malformed_or_failing_embedder_soft_fails(monkeypatch):
    _bank(monkeypatch)

    assert adapter.semantic_signal("query", embedder=lambda _text: None) is None
    assert adapter.semantic_signal("query", embedder=lambda _text: (_ for _ in ()).throw(RuntimeError())) is None


def test_default_path_refuses_remote_and_does_not_call_embed(monkeypatch):
    _bank(monkeypatch)
    calls = []
    monkeypatch.setenv("SONDER_ALLOW_REMOTE_OLLAMA", "1")
    monkeypatch.setattr(adapter.embeddings, "BASE", "https://remote.invalid:11434")
    monkeypatch.setattr(
        adapter.embeddings, "embed", lambda *_args, **_kwargs: calls.append(True)
    )
    assert adapter.semantic_signal("query") is None
    assert calls == []


@pytest.mark.parametrize("model", ["gpt-oss:120b-cloud", "qwen3-coder:480b-cloud", "cloud-general"])
def test_loopback_does_not_authorize_cloud_models(monkeypatch, model):
    _bank(monkeypatch)
    calls = []
    monkeypatch.setattr(adapter.embeddings, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(adapter.embeddings, "embed", lambda *args, **kwargs: calls.append(True))
    assert adapter.semantic_signal("private request", model=model) is None
    assert calls == []


def test_default_path_honors_membership_revocation(monkeypatch):
    _bank(monkeypatch)
    calls = []
    monkeypatch.setattr(adapter.embeddings, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(adapter.embeddings, "endpoint_is_loopback", lambda _base: True)
    monkeypatch.setattr(adapter.ollama_endpoint, "_default_embedding_operation", lambda: _denied())
    monkeypatch.setattr(adapter.embeddings, "embed", lambda *_args, **_kwargs: calls.append(True))
    assert adapter.semantic_signal("query") is None
    assert calls == []


def test_successful_cache_invalidates_for_model_bank_and_fake_identity(monkeypatch):
    _bank(monkeypatch)
    calls = []

    def fake(text):
        calls.append(text)
        return [1.0, 0.0] if "code" in text else [0.0, 1.0]

    assert adapter.semantic_signal("code query", embedder=fake, model="bge-m3")["tier"] == "code"
    assert len(calls) == 11
    assert adapter.semantic_signal("code query", embedder=fake, model="bge-m3")["tier"] == "code"
    assert len(calls) == 12
    monkeypatch.setattr(semantic_tier, "BANK_DIGEST", "changed-bank")
    assert adapter.semantic_signal("code query", embedder=fake, model="bge-m3")["tier"] == "code"
    assert len(calls) == 23
    assert adapter.semantic_signal("code query", embedder=fake, model="another-model")["tier"] == "code"
    assert len(calls) == 34
    assert adapter.semantic_signal("code query", embedder=lambda text: fake(text), model="another-model")["tier"] == "code"
    assert len(calls) == 45


def test_transient_failure_can_retry_and_internal_typeerror_is_not_retried(monkeypatch):
    _bank(monkeypatch)
    calls = []

    def fake(text):
        calls.append(text)
        if len(calls) == 1:
            raise TypeError("failure inside embedder")
        return [1.0, 0.0] if "code" in text else [0.0, 1.0]

    assert adapter.semantic_signal("code query", embedder=fake) is None
    assert len(calls) == 1
    assert adapter.semantic_signal("code query", embedder=fake)["tier"] == "code"
    assert len(calls) == 12


def test_timeout_does_not_multiply_workers(monkeypatch):
    _bank(monkeypatch)
    started = time.monotonic()
    calls = []
    release = threading.Event()

    def slow(_text, **_kwargs):
        calls.append(True)
        release.wait(2)
        return [1.0, 0.0]

    monkeypatch.setattr(adapter.embeddings, "embed", slow)
    monkeypatch.setattr(adapter.embeddings, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(adapter.embeddings, "endpoint_is_loopback", lambda _base: True)
    monkeypatch.setattr(adapter.ollama_endpoint, "_default_embedding_operation", lambda: _allowed())
    monkeypatch.setattr(adapter, "CALLER_TIMEOUT", 0.05)
    monkeypatch.setattr(adapter, "BUILD_DEADLINE", 0.01)
    job = None
    try:
        assert adapter.semantic_signal("query", model="bge-m3") is None
        job = adapter._flight
        assert job is not None
        monkeypatch.setattr(semantic_tier, "BANK_DIGEST", "new-bank-while-busy")
        assert adapter.semantic_signal("query", model="nomic-embed-text") is None
        assert time.monotonic() - started < 0.5
        assert len(calls) == 1
    finally:
        release.set()
        if job is not None:
            assert job.event.wait(2), "worker leaked beyond test teardown"
    assert len(calls) == 1  # Deadline prevents any further bank embeddings.
    assert adapter._flight is None


def test_worker_start_failure_clears_single_flight(monkeypatch):
    _bank(monkeypatch)
    class BrokenThread:
        def __init__(self, **_kwargs):
            pass

        def start(self):
            raise RuntimeError("thread unavailable")

    monkeypatch.setattr(adapter.threading, "Thread", BrokenThread)
    assert adapter.semantic_signal("query", embedder=lambda _text: [1.0, 0.0, 0.0, 0.0]) is None
    with adapter._cache_lock:
        assert adapter._flight is None


def test_production_cache_requires_query_revision_and_dimension_match(monkeypatch):
    _bank(monkeypatch)
    state = {"revision": "r1"}
    calls = []

    def fake_embed(_text, **_kwargs):
        calls.append(state["revision"])
        text = _text.lower()
        if "code" in text:
            return [1.0, 0.0, 0.0, 0.0]
        if "reason" in text:
            return [0.0, 1.0, 0.0, 0.0]
        if "vision" in text:
            return [0.0, 0.0, 1.0, 0.0]
        return [0.0, 0.0, 0.0, 1.0]

    def fake_provenance(_vector):
        return {"model": "bge-m3:latest", "revision": state["revision"], "dimension": 4}

    monkeypatch.setattr(adapter.embeddings, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(adapter.embeddings, "endpoint_is_loopback", lambda _base: True)
    monkeypatch.setattr(adapter.ollama_endpoint, "_default_embedding_operation", lambda: _allowed())
    monkeypatch.setattr(adapter.embeddings, "embed", fake_embed)
    monkeypatch.setattr(adapter.embeddings, "provenance", fake_provenance)

    assert adapter.semantic_signal("code query", model="bge-m3")["tier"] == "code"
    first_count = len(calls)
    assert adapter.semantic_signal("code query", model="bge-m3")["tier"] == "code"
    assert len(calls) == first_count + 1
    state["revision"] = "r2"
    assert adapter.semantic_signal("code query", model="bge-m3")["tier"] == "code"
    assert len(calls) == first_count * 2 + 1


def test_model_revision_changing_during_bank_build_abstains(monkeypatch):
    _bank(monkeypatch)
    calls = []

    def fake_embed(text, **kwargs):
        calls.append(kwargs["base"])
        return [1.0, 0.0] if "code" in text else [0.0, 1.0]

    monkeypatch.setattr(adapter.embeddings, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(adapter.ollama_endpoint, "_default_embedding_operation", lambda: _allowed())
    monkeypatch.setattr(adapter.embeddings, "embed", fake_embed)
    monkeypatch.setattr(adapter.embeddings, "provenance", lambda vector: {
        "model": "bge-m3:latest", "dimension": 2,
        "revision": "r1" if len(calls) == 1 else "r2",
    })
    assert adapter.semantic_signal("code query", model="bge-m3") is None
    assert len(calls) == 2
    assert not adapter._cache


class _allowed:
    def __enter__(self):
        return True

    def __exit__(self, *_args):
        return False


class _denied:
    def __enter__(self):
        return False

    def __exit__(self, *_args):
        return False


def _head_file(tmp_path, **overrides):
    import json

    payload = {
        "labels": ["fast", "general", "code", "reasoning", "vision"],
        "W": [[0.0, 0.0], [0.0, 0.0], [8.0, 0.0], [0.0, 8.0], [0.0, 0.0]],
        "b": [0.0] * 5,
        "embedding_model": "nomic-embed-text:latest",
        "input_prefix": "classification: ",
    }
    payload.update(overrides)
    path = tmp_path / "head.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def test_trained_head_classifies_with_one_embedding_and_its_prefix(monkeypatch, tmp_path):
    _bank(monkeypatch)
    monkeypatch.setenv("SONDER_SEMANTIC_TIER_HEAD", str(_head_file(tmp_path)))
    seen = []

    def fake(text):
        seen.append(text)
        return [1.0, 0.0]

    result = adapter.semantic_signal("fix this bug", embedder=fake, model="nomic-embed-text")
    assert result["tier"] == "code" and result["method"] == "trained-head"
    assert seen == ["classification: fix this bug"], "one embedding, no centroid bank"


def test_trained_head_abstains_when_unsure_and_never_picks_vision(monkeypatch, tmp_path):
    _bank(monkeypatch)
    monkeypatch.setenv("SONDER_SEMANTIC_TIER_HEAD", str(_head_file(tmp_path)))
    # Equal code/reasoning logits: no margin, so no semantic signal.
    assert adapter.semantic_signal("x", embedder=lambda _t: [1.0, 1.0], model="nomic-embed-text") is None
    vision = _head_file(tmp_path, W=[[0.0, 0.0]] * 4 + [[9.0, 0.0]])
    monkeypatch.setenv("SONDER_SEMANTIC_TIER_HEAD", str(vision))
    assert adapter.semantic_signal("x", embedder=lambda _t: [1.0, 0.0], model="nomic-embed-text") is None


def test_a_head_for_another_embedding_model_falls_back_to_centroids(monkeypatch, tmp_path):
    _bank(monkeypatch)
    monkeypatch.setenv("SONDER_SEMANTIC_TIER_HEAD", str(_head_file(tmp_path, embedding_model="bge-m3:latest")))
    calls = []

    def fake(text):
        calls.append(text)
        return [1.0, 0.0] if "code" in text else [0.0, 1.0]

    result = adapter.semantic_signal("code query", embedder=fake, model="nomic-embed-text")
    assert result is not None and result.get("method") != "trained-head"
    assert len(calls) > 1, "the centroid bank was embedded"


def test_the_shipped_head_loads_and_carries_no_user_data():
    head = adapter.trained_head()
    assert head is not None and head.embedding_model == "nomic-embed-text:latest"
    assert set(head.labels) == {"fast", "general", "code", "reasoning", "vision"}
    import json

    meta = json.loads(adapter._SHIPPED_HEAD.read_text(encoding="utf-8"))
    assert set(meta["trained_on"]) <= {"bank", "synthetic"}
