from __future__ import annotations

from sonder_runtime.adapters.inference import ollama_endpoint


def test_typed_endpoint_wins_over_legacy_environment(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11999")
    ollama_endpoint.configure_typed_endpoint("http://127.0.0.1:11434")
    try:
        assert ollama_endpoint.normalize() == "http://127.0.0.1:11434"
    finally:
        ollama_endpoint.reset_typed_endpoint()


def test_endpoint_restores_environment_compatibility_fallback(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "http://127.0.0.1:11999")
    ollama_endpoint.reset_typed_endpoint()
    assert ollama_endpoint.normalize() == "http://127.0.0.1:11999"
