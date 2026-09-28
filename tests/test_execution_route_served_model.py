"""The execution-decision header names the model that actually serves a tier.

Tiers bound to Sonder Inference are answered by the model the gateway sends
(SONDER_INFERENCE_TIER_MODELS, else SONDER_INFERENCE_MODEL), not by the
Ollama policy model, so the header must not name the policy model for them.
"""

import server
from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceUnreachable
from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest, ModelResponse


def _clear_provider_env(monkeypatch):
    for name in ("SONDER_MODEL_BACKEND", "SONDER_FAST_PROVIDER", "SONDER_GENERAL_PROVIDER",
                 "SONDER_CODE_PROVIDER", "SONDER_REASONING_PROVIDER", "SONDER_VISION_PROVIDER",
                 "SONDER_INFERENCE_MODEL", "SONDER_INFERENCE_TIER_MODELS"):
        monkeypatch.delenv(name, raising=False)


def test_inference_bound_tier_shows_the_served_model(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setitem(server.TIERS, "general", "sonder:latest")
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "qwen3:14b")
    header = server._execution_route_header("workbench", "host classifier", "r", None, "general")
    assert "  tier: general -> qwen3:14b (sonder_inference)" in header
    assert "sonder:latest" not in header


def test_inference_tier_model_map_wins_over_the_default(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "qwen3:14b")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "code=qwen2.5-coder:14b")
    header = server._execution_route_header("workbench", "s", "r", None, "code")
    assert "  tier: code -> qwen2.5-coder:14b (sonder_inference)" in header


def test_ollama_bound_tier_keeps_the_policy_model(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setitem(server.TIERS, "general", "sonder:latest")
    header = server._execution_route_header("workbench", "s", "r", None, "general")
    assert "  tier: general -> sonder:latest" in header
    assert "sonder_inference" not in header


def test_unreadable_inference_config_falls_back_to_the_policy_model(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setitem(server.TIERS, "general", "sonder:latest")
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "not-a-pair")
    header = server._execution_route_header("workbench", "s", "r", None, "general")
    assert "  tier: general -> sonder:latest" in header


def test_routed_work_header_names_ollama_after_inference_fallback(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setitem(server.TIERS, "code", "sonder:latest")
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "qwen3:14b")
    monkeypatch.setenv("SONDER_INFERENCE_FALLBACK", "ollama")

    class UnreachableInference:
        def generate(self, request, context):
            raise SonderInferenceUnreachable("connection refused")

    class LocalOllama:
        def generate(self, request, context):
            return ModelResponse(text="done", model="sonder:latest", tier=request.tier)

    gateway = PreSendFallbackGateway(UnreachableInference(), fallback=LocalOllama())

    def workbench_agent(**kwargs):
        gateway.generate(
            ModelRequest(prompt=kwargs["prompt"], tier=kwargs["tier"]),
            local_owner_context(correlation_id="route-header", source="mcp"),
        )
        return "work complete"

    monkeypatch.setattr(server, "workbench_agent", workbench_agent)
    output = server.route_work_request("Build the Flutter app.")
    assert "  tier: code -> sonder:latest (ollama)" in output
    assert "  tier: code -> qwen3:14b (sonder_inference)" not in output

    monkeypatch.setattr(server, "workbench_agent", lambda **_kwargs: "no model call")
    next_output = server.route_work_request("Build the Flutter app.")
    assert "  tier: code -> qwen3:14b (sonder_inference)" in next_output
