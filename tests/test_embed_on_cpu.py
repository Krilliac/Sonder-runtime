"""SONDER_EMBED_ON_CPU keeps the embedding model off the GPU (opt-in).

Measured 2026-09-28 on the workstation (RTX 5070 Ti 16 GB): the chat model
(Qwen3.8 27B) takes 12.8 GB of VRAM, so a GPU-loaded embedder evicted it and
every routing or memory embedding cost a ~20 s chat-model reload. With the
embedder on CPU (Ollama ``num_gpu: 0``) all stayed resident: embeds took
21-26 ms and the chat model was never evicted.
"""
from __future__ import annotations

import json

import pytest

from sonder_runtime.adapters import embeddings


class _Resp:
    def __init__(self, body):
        self._body = json.dumps(body).encode()

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def captured(monkeypatch):
    seen = []

    def fake_open(req, timeout=None):
        if getattr(req, "full_url", "").endswith("/api/embeddings"):
            seen.append(json.loads(req.data.decode()))
            return _Resp({"embedding": [0.1] * 768})
        raise OSError("no revision probe in this test")

    monkeypatch.setattr(embeddings.ollama_endpoint, "open_url", fake_open)
    monkeypatch.setattr(embeddings.embed_cache, "get", lambda *a, **k: None, raising=False)
    return seen


@pytest.mark.parametrize("value, on_cpu", [(None, False), ("0", False), ("1", True), ("true", True)])
def test_the_flag_adds_num_gpu_zero_only_when_opted_in(monkeypatch, captured, value, on_cpu):
    if value is None:
        monkeypatch.delenv("SONDER_EMBED_ON_CPU", raising=False)
    else:
        monkeypatch.setenv("SONDER_EMBED_ON_CPU", value)
    embeddings.embed("hello", timeout=5, base="http://127.0.0.1:11434", model="nomic-embed-text:latest")
    assert captured, "the embed request was made"
    body = captured[-1]
    assert body["model"] == "nomic-embed-text:latest" and body["prompt"] == "hello"
    assert (body.get("options") == {"num_gpu": 0}) is on_cpu
    if not on_cpu:
        assert "options" not in body
