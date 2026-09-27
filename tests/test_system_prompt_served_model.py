"""The chat system prompt names the model that actually serves a rung.

A tier bound to Sonder Inference is answered by the model the gateway sends
(SONDER_INFERENCE_TIER_MODELS, else SONDER_INFERENCE_MODEL), not by the Ollama
policy model.  The runtime identity block must therefore name the served
model, or the assistant introduces itself as the wrong model.
"""

import pytest

import server
from sonder_runtime.adapters.inference.served_tier_models import served_prompt_model


class _Built(Exception):
    """Stops the turn once the system prompt has been built."""


def _clear_provider_env(monkeypatch):
    for name in ("SONDER_MODEL_BACKEND", "SONDER_FAST_PROVIDER", "SONDER_GENERAL_PROVIDER",
                 "SONDER_CODE_PROVIDER", "SONDER_REASONING_PROVIDER", "SONDER_VISION_PROVIDER",
                 "SONDER_INFERENCE_MODEL", "SONDER_INFERENCE_TIER_MODELS"):
        monkeypatch.delenv(name, raising=False)


def _capture_prompt_model(monkeypatch):
    seen = {}

    def fake_build_system(system, trace, persona, model="", cloud=False, provider=None):
        seen["model"] = model
        seen["provider"] = provider
        seen["prompt"] = server._runtime_identity_block(model, cloud, provider)
        raise _Built()

    monkeypatch.setattr(server, "_build_system", fake_build_system)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    return seen


def _chat(tier):
    with pytest.raises(_Built):
        server._answer_with_history_impl("hello", [], tier=tier)


def _structured(tier):
    with pytest.raises(_Built):
        server.structured_answer_with_history("hello", [], {"type": "object"}, tier=tier)


@pytest.mark.parametrize("entry", [_chat, _structured])
def test_inference_bound_tier_names_the_served_model(monkeypatch, entry):
    _clear_provider_env(monkeypatch)
    monkeypatch.setitem(server.TIERS, "general", "sonder:latest")
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "qwen3:14b")
    seen = _capture_prompt_model(monkeypatch)
    entry("general")
    assert seen["model"] == "qwen3:14b"
    assert "`qwen3:14b`" in seen["prompt"]
    assert "sonder:latest" not in seen["prompt"]
    assert seen["provider"] == "sonder_inference"
    assert "served through Sonder Inference" in seen["prompt"]
    assert "Ollama" not in seen["prompt"]


def test_inference_tier_model_map_wins_over_the_default(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setitem(server.TIERS, "code", "sonder-coder:latest")
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "qwen3:14b")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "code=qwen2.5-coder:14b")
    seen = _capture_prompt_model(monkeypatch)
    _chat("code")
    assert seen["model"] == "qwen2.5-coder:14b"


@pytest.mark.parametrize("entry", [_chat, _structured])
def test_ollama_bound_tier_keeps_the_policy_model(monkeypatch, entry):
    _clear_provider_env(monkeypatch)
    monkeypatch.setitem(server.TIERS, "general", "sonder:latest")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "qwen3:14b")
    seen = _capture_prompt_model(monkeypatch)
    entry("general")
    assert seen["model"] == "sonder:latest"


@pytest.mark.parametrize("entry", [_chat, _structured])
def test_explicit_model_pin_keeps_the_pinned_model(monkeypatch, entry):
    _clear_provider_env(monkeypatch)
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "qwen3:14b")
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("pinned:7b", False, False, "model:pinned:7b"),
    )
    seen = _capture_prompt_model(monkeypatch)
    entry("model:pinned:7b")
    assert seen["model"] == "pinned:7b"


def test_unreadable_inference_config_falls_back_to_the_policy_model(monkeypatch):
    _clear_provider_env(monkeypatch)
    monkeypatch.setitem(server.TIERS, "general", "sonder:latest")
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "not-a-pair")
    seen = _capture_prompt_model(monkeypatch)
    _chat("general")
    assert seen["model"] == "sonder:latest"


def test_helper_leaves_non_inference_providers_and_cloud_alone():
    env = {"SONDER_INFERENCE_MODEL": "qwen3:14b"}
    assert served_prompt_model("pinned:7b", "general", None, env) == "pinned:7b"
    assert served_prompt_model("pinned:7b", "general", "openai_compatible", env) == "pinned:7b"
    assert served_prompt_model("sonder:latest", "general", "sonder_inference", env) == "qwen3:14b"


def test_helper_mirrors_the_gateway_tier_default():
    # bind_rung labels a blank tier "sonder", which no tier map can name, so
    # the gateway sends its default model.
    env = {"SONDER_INFERENCE_MODEL": "qwen3:14b", "SONDER_INFERENCE_TIER_MODELS": "general=a:1"}
    assert served_prompt_model("sonder:latest", "", "sonder_inference", env) == "qwen3:14b"
    assert served_prompt_model("sonder:latest", "general", "sonder_inference", env) == "a:1"


def test_helper_falls_back_when_config_is_unreadable():
    env = {"SONDER_INFERENCE_TIER_MODELS": "not-a-pair"}
    assert served_prompt_model("sonder:latest", "general", "sonder_inference", env) == (
        "sonder:latest"
    )
