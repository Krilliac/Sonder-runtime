"""The execution-decision header names the model that actually serves a tier.

Tiers bound to Sonder Inference are answered by the model the gateway sends
(SONDER_INFERENCE_TIER_MODELS, else SONDER_INFERENCE_MODEL), not by the
Ollama policy model, so the header must not name the policy model for them.
"""

import server


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
