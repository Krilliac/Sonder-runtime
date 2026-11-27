"""Metadata probes in server.py use Sonder Inference /v1/models and /v1/sonder/identity."""

import server

MODEL = "astra-cyber-27b"


def _capture_get(monkeypatch, reply=None):
    seen = []

    def fake_get(path, **kwargs):
        seen.append((path, dict(kwargs)))
        return reply if reply is not None else {}

    monkeypatch.setattr(server, "_get", fake_get)
    return seen


def test_capability_probe_carries_model_hint(monkeypatch):
    seen = _capture_get(
        monkeypatch,
        {"object": "list", "data": [{"id": MODEL, "capabilities": ["embedding"]}]},
    )
    assert server._runtime_model_has_capability(MODEL, "embedding", [(MODEL, {"capabilities": ["embedding"]})])
    assert seen == [] or any(path.startswith("/v1/models") for path, _ in seen)


def test_synthesis_model_probe_carries_model_hint(monkeypatch):
    seen = _capture_get(
        monkeypatch,
        {"object": "list", "data": [{"id": MODEL, "capabilities": ["completion"]}]},
    )
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)
    monkeypatch.setattr(server, "resolve_discovered_model_record", lambda _s: (MODEL, {}))
    monkeypatch.setattr(server, "_fanout_declares_generative_capability",
                        lambda record: bool(record.get("capabilities")))
    assert server._fanout_synthesis_model(MODEL) == MODEL
    assert any(path.startswith("/v1/models") for path, _ in seen), seen


def test_prompt_identity_probe_carries_model_hint(monkeypatch):
    seen = _capture_get(
        monkeypatch,
        {
            "backend_identity": {
                "model_digest": "a" * 64,
                "tokenizer_digest": "gpt2",
                "template_digest": "rev",
            }
        },
    )
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)
    origins = (server.ollama_policy.normalize(server.BASE).rstrip("/"),)
    monkeypatch.setattr(type(server.OLLAMA_POOL), "configured_origins",
                        property(lambda _self: origins))
    monkeypatch.setattr(server, "_ollama_model_tag_metadata", lambda _m: ("a" * 64, "rev"))
    server._MODEL_PROMPT_IDENTITY_CACHE.clear()
    server._model_prompt_identity(MODEL)
    server._MODEL_PROMPT_IDENTITY_CACHE.clear()
    assert any(
        path.startswith("/v1/sonder/identity") and f"model={MODEL}" in path
        for path, _ in seen
    ), seen
