"""Revision-pinned embedding cache: exact keys, LRU bounds, fail-soft."""
import json

import pytest

import embed_cache
import embeddings as e


class FakeResponse:
    def __init__(self, data):
        self.data = data

    def read(self):
        return self.data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


@pytest.fixture(autouse=True)
def _cache_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_EMBED_CACHE", "1")
    monkeypatch.setenv("SONDER_EMBED_CACHE_DB", str(tmp_path / "cache.db"))
    embed_cache.reset_for_tests()
    yield
    embed_cache.reset_for_tests()


def test_put_get_roundtrip_is_exact_for_float32():
    vector = [0.25, -0.5, 1.0, 0.125]
    assert embed_cache.put("text", "model:latest", "rev-1", vector)
    assert embed_cache.get("text", "model:latest", "rev-1") == vector


def test_revision_and_identity_changes_miss():
    embed_cache.put("text", "model:latest", "rev-1", [1.0, 2.0])
    assert embed_cache.get("text", "model:latest", "rev-2") is None
    assert embed_cache.get("text", "other:latest", "rev-1") is None
    assert embed_cache.get("other text", "model:latest", "rev-1") is None


def test_disabled_cache_never_stores_or_serves(monkeypatch):
    monkeypatch.setenv("SONDER_EMBED_CACHE", "0")
    assert not embed_cache.put("text", "model:latest", "rev-1", [1.0])
    assert embed_cache.get("text", "model:latest", "rev-1") is None


def test_missing_revision_is_never_cached():
    assert not embed_cache.put("text", "model:latest", "", [1.0])
    assert embed_cache.get("text", "model:latest", "") is None


def test_lru_eviction_bounds_row_count(monkeypatch):
    monkeypatch.setenv("SONDER_EMBED_CACHE_MAX", "100")
    for index in range(120):
        embed_cache.put("text-%d" % index, "model:latest", "rev-1", [1.0])
    assert embed_cache.stats()["rows"] <= 100
    # newest rows survive eviction
    assert embed_cache.get("text-119", "model:latest", "rev-1") == [1.0]


def test_embed_serves_second_call_from_cache(monkeypatch):
    calls = []

    def fake_open_url(request, timeout=30, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        if url.endswith("/api/embeddings"):
            calls.append(url)
        return FakeResponse(json.dumps(
            {"embedding": [0.25, 0.5, 0.25]}
        ).encode("utf-8"))

    monkeypatch.setattr(e.ollama_endpoint, "open_url", fake_open_url)
    monkeypatch.setattr(e, "current_revision", lambda **k: "rev-current")
    monkeypatch.setattr(
        e, "refresh_runtime_revision", lambda **k: "rev-current",
    )
    monkeypatch.setattr(e, "expected_dimension", lambda model=None: 3)

    first = e.embed("cache integration probe")
    assert first == pytest.approx([0.25, 0.5, 0.25])
    assert e.provenance(first)["provider"] == "ollama"
    assert len(calls) == 1

    second = e.embed("cache integration probe")
    assert second == first
    assert e.provenance(second)["provider"] == "cache"
    assert len(calls) == 1  # no second HTTP call


def test_embed_cache_invalidates_on_revision_change(monkeypatch):
    revision = {"value": "rev-a"}

    def fake_open_url(request, timeout=30, **kwargs):
        return FakeResponse(json.dumps(
            {"embedding": [1.0, 0.0, 0.0]}
        ).encode("utf-8"))

    monkeypatch.setattr(e.ollama_endpoint, "open_url", fake_open_url)
    monkeypatch.setattr(
        e, "current_revision", lambda **k: revision["value"],
    )
    monkeypatch.setattr(
        e, "refresh_runtime_revision", lambda **k: revision["value"],
    )
    monkeypatch.setattr(e, "expected_dimension", lambda model=None: 3)

    assert e.embed("revision probe") is not None
    revision["value"] = "rev-b"
    assert e.embed("revision probe") is not None
    assert e.provenance(e._EMBED_STATE.vector)["provider"] == "ollama"
