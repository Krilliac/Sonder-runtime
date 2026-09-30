"""Specialized helpers must not load an Ollama model for another provider."""
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse


def test_typed_vision_refuses_non_ollama_binding_before_model_resolution(monkeypatch):
    from sonder_runtime.adapters.inference.ollama_vision import OllamaVisionGateway
    from sonder_runtime.application.ports.vision_gateway import VisionRequest
    from sonder_runtime.domain.common.errors import Forbidden

    monkeypatch.setenv("SONDER_VISION_PROVIDER", "sonder-inference")
    gateway = OllamaVisionGateway(
        target_resolver=lambda *_args: pytest.fail("vision resolved an Ollama model"),
        transport=lambda *_args: pytest.fail("image was sent to Ollama"),
    )
    request = VisionRequest("describe", b"image", "image/png")
    with pytest.raises(Forbidden, match="Ollama-bound"):
        gateway.analyze(request, local_owner_context(correlation_id="vision"))


def test_nightly_code_readiness_uses_the_bound_provider_and_local_consent(monkeypatch):
    import server
    from scripts import nightly_self_improve

    calls = []

    class Gateway:
        def generate(self, request, context):
            calls.append((request, context))
            return ModelResponse("READY", "bound-code", request.tier)

    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform("sonder_inference"), model_gateway=Gateway())
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_application", lambda: graph)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_auto_model_context", lambda *_args: pytest.fail("Ollama context probe"))
    monkeypatch.setattr(nightly_self_improve, "_local_ollama_json", lambda *_args: pytest.fail("Ollama prewarm"))
    assert nightly_self_improve._prewarm_code_model(server) == "ready provider=sonder_inference"
    assert len(calls) == 1
    assert calls[0][0].tier == "code"
    assert calls[0][1].cloud_allowed is False


def test_ensemble_alias_carries_resolved_provider_tier(monkeypatch):
    import server

    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform("sonder_inference"))
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_serve_target", lambda *_args: ("same-model", False, True, "general"))
    assert server._ensemble_targets("sonder") == ([("general", "same-model")], [])
    # Historical Ollama tier labels remain unchanged.
    graph.provider_bindings = ProviderBindings.uniform("ollama")
    assert server._ensemble_targets("sonder") == ([("sonder", "same-model")], [])
