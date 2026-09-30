"""Thinking forwarding, sampling defaults, telemetry and GPU residency for Sonder Inference."""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sonder_runtime.adapters.inference import gpu_residency, request_tuning
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
)
from sonder_runtime.adapters.inference.telemetry import from_openai_compatible
from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env
from sonder_runtime.application.chat import provider_bridge as bridge
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import (
    InferenceTelemetry,
    ModelRequest,
    ModelResponse,
)
from sonder_runtime.domain.common.errors import InvalidInput

import sonder_doctor


def _health(**extra):
    document = {
        "status": "ready", "api_version": 1, "version": "0.1.0", "synthetic": False,
        "backends": [{"name": "llamaserver", "available": True, "capabilities": ["streaming"]}],
        "models": [{"id": "qwen3.8:27b", "backend": "llamaserver", "default": True}],
    }
    document.update(extra)
    return document


class Fake:
    def __init__(self, health):
        self.health = health
        self.posts = []

    def get(self, url, headers, timeout):
        return 200, json.dumps(self.health).encode()

    def post(self, url, payload, headers, timeout):
        self.posts.append(json.loads(json.dumps(payload)))
        return {
            "model": "qwen3.8:27b",
            "choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
            "sonder": {"api_version": 1},
        }


def _generate(health, options, env=None, model="default"):
    fake = Fake(health)
    gateway = SonderInferenceGateway(
        SonderInferenceConfig(model=model), transport=fake.post, get_transport=fake.get,
        env=env or {}, wall_clock=lambda: datetime(2026, 9, 30, tzinfo=timezone.utc),
    )
    gateway.generate(
        ModelRequest(prompt="hi", tier="general", options=options),
        local_owner_context(correlation_id="t", source="http", timeout_seconds=10),
    )
    return fake.posts[0]


# -- thinking --------------------------------------------------------------------


@pytest.mark.parametrize("health", [
    _health(features=["chat_template_kwargs"]),
    _health(sonder={"api_version": 1, "features": ["thinking"]}),
    _health(backends=[{"name": "llamaserver", "capabilities": ["streaming", "thinking"]}]),
    _health(models=[{"id": "qwen3.8:27b", "default": True, "capabilities": ["enable_thinking"]}]),
])
def test_advertised_support_forwards_think_as_enable_thinking(health):
    assert _generate(health, {"think": False})["chat_template_kwargs"] == {"enable_thinking": False}
    assert _generate(health, {"think": True})["chat_template_kwargs"] == {"enable_thinking": True}


def test_without_support_false_is_dropped_and_true_refused():
    assert "chat_template_kwargs" not in _generate(_health(), {"think": False})
    with pytest.raises(InvalidInput, match="thinking"):
        _generate(_health(), {"think": True})


def test_operator_override_wins_over_detection():
    assert _generate(_health(), {"think": False}, env={"SONDER_INFERENCE_THINKING": "on"})[
        "chat_template_kwargs"] == {"enable_thinking": False}
    assert "chat_template_kwargs" not in _generate(
        _health(features=["thinking"]), {"think": False}, env={"SONDER_INFERENCE_THINKING": "off"},
    )
    with pytest.raises(InvalidInput):
        _generate(_health(), {"think": False}, env={"SONDER_INFERENCE_THINKING": "maybe"})


def test_bridge_carries_think_only_for_thinking_providers():
    payload = {"messages": [{"role": "user", "content": "hi"}], "think": False}
    request = bridge.model_request_from_ollama_payload(payload, tier="general", provider="sonder_inference")
    assert request.options["think"] is False
    request = bridge.model_request_from_ollama_payload(
        dict(payload, think=True), tier="general", provider="sonder_inference",
    )
    assert request.options["think"] is True
    # Every other provider keeps the historical refusal / drop.
    other = bridge.model_request_from_ollama_payload(payload, tier="general", provider="openrouter")
    assert "think" not in other.options
    with pytest.raises(bridge.UnsupportedProviderFeature):
        bridge.model_request_from_ollama_payload(dict(payload, think=True), tier="general")


# -- sampling defaults --------------------------------------------------------------


def test_sampling_defaults_are_off_by_default():
    payload = _generate(_health(), {"temperature": 0.2})
    assert payload == {
        "model": "default", "messages": [{"role": "user", "content": "hi"}],
        "stream": False, "temperature": 0.2,
    }


def test_enabled_defaults_fill_only_unset_fields_from_the_family_row():
    env = {"SONDER_INFERENCE_SAMPLING_DEFAULTS": "1"}
    thinking = _generate(_health(), {"temperature": 0.2}, env=env)
    # "default" resolves to the health document's default model (qwen3.8).
    assert thinking["temperature"] == 0.2  # caller-set, never overridden
    assert (thinking["top_p"], thinking["top_k"], thinking["min_p"],
            thinking["presence_penalty"]) == (0.95, 20, 0.0, 0.0)
    non_thinking = _generate(_health(features=["thinking"]), {"think": False, "top_k": 50}, env=env)
    assert non_thinking["top_k"] == 50
    assert (non_thinking["temperature"], non_thinking["top_p"], non_thinking["min_p"],
            non_thinking["presence_penalty"]) == (0.7, 0.8, 0.0, 1.5)


def test_think_false_that_is_not_forwarded_uses_the_template_default_row():
    env = {"SONDER_INFERENCE_SAMPLING_DEFAULTS": "1"}
    payload = _generate(_health(), {"think": False}, env=env)
    assert payload["temperature"] == 1.0 and payload["presence_penalty"] == 0.0


@pytest.mark.parametrize("model,family", [
    ("qwen3.8:27b", "qwen3.x"), ("Qwen3-32B-Q4_K_M.gguf", "qwen3.x"),
    ("qwen3-coder:30b", None), ("qwen2.5:7b", None), ("qwen30:1b", None), ("llama3", None),
])
def test_family_matching(model, family):
    payload: dict = {}
    applied = request_tuning.apply_sampling_defaults(
        payload, model, thinking=None, env={"SONDER_INFERENCE_SAMPLING_DEFAULTS": "on"},
    )
    assert applied == family
    assert bool(payload) is (family is not None)


def test_table_override_and_validation():
    table = [{"family": "mine", "match": "^m$", "non_thinking": {"min_p": 0.1, "top_k": 7}}]
    env = {"SONDER_INFERENCE_SAMPLING_DEFAULTS": "1", "SONDER_INFERENCE_SAMPLING_TABLE": json.dumps(table)}
    payload: dict = {}
    assert request_tuning.apply_sampling_defaults(payload, "m", thinking=False, env=env) == "mine"
    assert payload == {"min_p": 0.1, "top_k": 7}
    for bad in ("not json", "{}", json.dumps([{"family": "x", "match": "("}]),
                json.dumps([{"family": "x", "match": "x", "thinking": {"mirostat": 1}}])):
        with pytest.raises(InvalidInput):
            request_tuning.sampling_families({"SONDER_INFERENCE_SAMPLING_TABLE": bad})


# -- telemetry ---------------------------------------------------------------------


def test_openai_compatible_telemetry_reads_cache_ttft_and_draft_counts():
    telemetry = from_openai_compatible(with_usage=True, payload={
        "usage": {"prompt_tokens": 100, "completion_tokens": 20,
                  "prompt_tokens_details": {"cached_tokens": 90}},
        "timings": {"ttft_ms": 12.5, "cache_n": 1, "draft_n": 10, "draft_n_accepted": 7},
    })
    assert (telemetry.prompt_tokens, telemetry.prompt_cached_tokens,
            telemetry.prompt_uncached_tokens) == (100, 90, 10)
    assert telemetry.output_tokens == 20
    assert telemetry.ttft_ms == 12.5
    assert (telemetry.draft_tokens, telemetry.draft_accepted_tokens) == (10, 7)


def test_cache_n_is_the_fallback_and_absence_stays_unknown():
    telemetry = from_openai_compatible({"usage": {"prompt_tokens": 50}, "timings": {"cache_n": 0}}, with_usage=True)
    assert (telemetry.prompt_cached_tokens, telemetry.prompt_uncached_tokens) == (0, 50)
    unknown = from_openai_compatible({"usage": {"prompt_tokens": 50}, "timings": {"prompt_n": 50}}, with_usage=True)
    assert unknown.prompt_cached_tokens is None and unknown.prompt_uncached_tokens is None
    assert unknown.ttft_ms is None and unknown.draft_tokens is None
    # timings.prompt_n is never read as the prompt total (llama.cpp: uncached only).
    assert from_openai_compatible({"timings": {"prompt_n": 50}}, with_usage=True) is None
    # Generic OpenAI-compatible peers keep timings-only telemetry.
    assert from_openai_compatible({"usage": {"prompt_tokens": 50, "completion_tokens": 3}}) is None


def test_inconsistent_counts_are_dropped_not_clamped():
    telemetry = from_openai_compatible(with_usage=True, payload={
        "usage": {"prompt_tokens": 5, "prompt_tokens_details": {"cached_tokens": 9}},
        "timings": {"draft_n": 2, "draft_n_accepted": 3},
    })
    assert telemetry.prompt_cached_tokens is None
    assert telemetry.draft_tokens is None and telemetry.draft_accepted_tokens is None


def test_ollama_shape_copies_provider_cache_counts():
    response = ModelResponse(
        text="x", model="m", tier="general", tokens_in=10, tokens_out=2,
        telemetry=InferenceTelemetry(prompt_cached_tokens=6),
    )
    assert bridge.ollama_shape(response)["prompt_eval_cached_count"] == 6
    unknown = ModelResponse(text="x", model="m", tier="general", tokens_in=10, tokens_out=2)
    assert "prompt_eval_cached_count" not in bridge.ollama_shape(unknown)


# -- residency and GPU sharing -------------------------------------------------------


def test_keep_alive_is_unchanged_unless_the_flag_is_on():
    calls = []

    def primary():
        calls.append(1)
        return "big:27b"

    assert gpu_residency.keep_alive_for("big:27b", "2m", primary=primary, env={}) == "2m"
    assert calls == []
    on = {"SONDER_KEEP_PRIMARY_RESIDENT": "1"}
    assert gpu_residency.keep_alive_for("big:27b", "2m", primary=primary, env=on) == -1
    assert gpu_residency.keep_alive_for("small:4b", "2m", primary=primary, env=on) == "2m"

    def broken():
        raise RuntimeError("policy unavailable")

    assert gpu_residency.keep_alive_for("big:27b", "2m", primary=broken, env=on) == "2m"


INFERENCE_GENERAL = {
    "SONDER_GENERAL_PROVIDER": "sonder_inference", "SONDER_CODE_PROVIDER": "sonder_inference",
    "SONDER_GENERAL": "qwen3.8:27b", "SONDER_CODE": "qwen3.8:27b", "SONDER_FAST": "qwen3:4b",
    "OLLAMA_HOST": "127.0.0.1:11434",
}


def _gpu_check(env):
    return sonder_doctor._check_sonder_inference_gpu(env=env)


def test_doctor_warns_about_every_local_gpu_contender():
    result = _gpu_check(INFERENCE_GENERAL)
    assert result["status"] == "warn"
    assert "qwen3:4b" in result["detail"]  # fast tier stays on local Ollama
    assert "SONDER_EMBED_ON_CPU=1" in result["detail"]


def test_doctor_is_ok_when_nothing_else_shares_the_gpu():
    env = dict(INFERENCE_GENERAL, SONDER_MODEL_BACKEND="sonder_inference",
               SONDER_EMBEDDING_PROVIDER="ollama", SONDER_EMBED_ON_CPU="1")
    env.pop("SONDER_GENERAL_PROVIDER")
    env.pop("SONDER_CODE_PROVIDER")
    assert _gpu_check(env)["status"] == "ok"


def test_doctor_ignores_a_remote_inference_server_and_skips_when_unbound():
    remote = dict(INFERENCE_GENERAL, SONDER_INFERENCE_BASE_URL="https://gpu-box:11437",
                  SONDER_ALLOW_REMOTE_INFERENCE="1", SONDER_INFERENCE_API_KEY="k",
                  SONDER_EMBED_ON_CPU="1")
    assert _gpu_check(remote)["status"] == "ok"
    assert _gpu_check({})["status"] == "skipped"


def test_doctor_warns_when_pinning_competes_with_other_local_models():
    env = {"SONDER_KEEP_PRIMARY_RESIDENT": "1", "SONDER_GENERAL": "big:27b",
           "SONDER_FAST": "small:4b", "OLLAMA_HOST": "127.0.0.1:11434"}
    result = _gpu_check(env)
    assert result["status"] == "warn" and "SONDER_KEEP_PRIMARY_RESIDENT" in result["detail"]


def test_local_ollama_tiers_exclude_cloud_and_other_providers():
    env = dict(INFERENCE_GENERAL, SONDER_FAST="kimi:cloud")
    models = gpu_residency.local_ollama_tier_models(env, provider_bindings_from_env(env))
    assert models == {}
