"""Ownership and behavior tests for packaged model-gateway selection."""

from __future__ import annotations

import pytest

from sonder_runtime.adapters.model_gateway_factory import build_model_gateway
from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway
from sonder_runtime.adapters.inference.openai_compat_gateway import OpenAICompatibleGateway
from sonder_runtime.bootstrap import app as bootstrap_app
from sonder_runtime.domain.common.errors import InvalidInput


def test_factory_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("SONDER_MODEL_BACKEND", raising=False)
    assert isinstance(build_model_gateway(), OllamaGateway)


def test_factory_normalizes_openai_compatible_aliases(monkeypatch):
    for backend in ("openai", " OPENAI-Compatible ", "llamacpp", "vllm"):
        monkeypatch.setenv("SONDER_MODEL_BACKEND", backend)
        assert isinstance(build_model_gateway(), OpenAICompatibleGateway)


def test_factory_accepts_explicit_ollama_backend():
    assert isinstance(build_model_gateway(backend="  Ollama "), OllamaGateway)


def test_factory_rejects_unknown_backend_instead_of_defaulting_to_ollama():
    # A typo in SONDER_MODEL_BACKEND must not silently route requests to a
    # different transport than the operator configured.
    with pytest.raises(InvalidInput):
        build_model_gateway(backend="openia")


def test_factory_rejects_unknown_backend_from_env(monkeypatch):
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "vllmm")
    with pytest.raises(InvalidInput):
        build_model_gateway()


def test_bootstrap_private_selector_preserves_identity_compatibility():
    assert bootstrap_app._build_model_gateway is build_model_gateway


def test_application_graph_uses_packaged_selector(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "policy.json"))
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "openai")
    bootstrap_app.reset_for_tests()
    try:
        assert isinstance(
            bootstrap_app.build_application().model_gateway,
            OpenAICompatibleGateway,
        )
    finally:
        bootstrap_app.reset_for_tests()
