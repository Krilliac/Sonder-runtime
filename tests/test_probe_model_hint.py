"""Every /api/show probe in server.py carries the resolved model as routing hint.

The pool routes on payload["model"]; a probe with only "name" is sent without
the hint, so a synthesized or aliased model can be probed on another worker.
"""
import pytest

import server

MODEL = "astra-cyber-27b"


def _capture(monkeypatch, reply=None):
    seen = []

    def fake_post(path, payload, **_kwargs):
        seen.append((path, dict(payload)))
        return reply if reply is not None else {}

    monkeypatch.setattr(server, "_post", fake_post)
    return seen


def _show_payloads(seen):
    return [payload for path, payload in seen if path == "/api/show"]


def test_capability_probe_carries_model_hint(monkeypatch):
    seen = _capture(monkeypatch, {"capabilities": ["embedding"]})
    assert server._runtime_model_has_capability(MODEL, "embedding", [(MODEL, {})])
    payloads = _show_payloads(seen)
    assert payloads, "no /api/show probe was sent"
    assert all(p.get("model") == MODEL for p in payloads), payloads


def test_synthesis_model_probe_carries_model_hint(monkeypatch):
    seen = _capture(monkeypatch, {"capabilities": ["completion"]})
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)
    monkeypatch.setattr(server, "resolve_discovered_model_record", lambda _s: (MODEL, {}))
    monkeypatch.setattr(server, "_fanout_declares_generative_capability",
                        lambda record: bool(record.get("capabilities")))
    assert server._fanout_synthesis_model(MODEL) == MODEL
    payloads = _show_payloads(seen)
    assert payloads, "no /api/show probe was sent"
    assert all(p.get("model") == MODEL for p in payloads), payloads


def test_prompt_identity_probe_carries_model_hint(monkeypatch):
    seen = _capture(monkeypatch, {"template": "t", "modified_at": "rev",
                                  "model_info": {"tokenizer.ggml.model": "gpt2"}})
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)
    origins = (server.ollama_policy.normalize(server.BASE).rstrip("/"),)
    monkeypatch.setattr(type(server.OLLAMA_POOL), "configured_origins",
                        property(lambda _self: origins))
    monkeypatch.setattr(server, "_ollama_model_tag_metadata", lambda _m: ("digest", "rev"))
    server._MODEL_PROMPT_IDENTITY_CACHE.clear()
    server._model_prompt_identity(MODEL)
    server._MODEL_PROMPT_IDENTITY_CACHE.clear()
    payloads = _show_payloads(seen)
    assert payloads, "no /api/show probe was sent"
    assert all(p.get("model") == MODEL for p in payloads), payloads
