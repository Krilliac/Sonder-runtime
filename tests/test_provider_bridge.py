"""Pure conversion, routing and error classification for the HTTP chat bridge."""
from types import SimpleNamespace

import pytest

from sonder_runtime.application.chat import provider_bridge as bridge
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.routing import tier_escalation
from sonder_runtime.domain.common.errors import (
    Cancelled,
    CapacityExceeded,
    DeadlineExceeded,
    DependencyUnavailable,
    Forbidden,
    InternalFailure,
    InvalidInput,
)

BINDINGS = SimpleNamespace(
    default_generation_provider="openai_compatible",
    tier_providers={
        "fast": "ollama", "general": "openai_compatible", "code": "sonder_inference",
        "reasoning": "ollama", "vision": "ollama",
    },
    embedding_provider="ollama",
)


def _payload(**extra):
    payload = {
        "model": "qwen2.5-coder:7b",
        "messages": [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "earlier question"},
            {"role": "assistant", "content": "earlier answer"},
            {"role": "user", "content": "current question"},
        ],
        "stream": False,
        "options": {"temperature": 0.3, "num_predict": 64, "num_ctx": 8192,
                    "num_thread": 8, "num_gpu": 1},
        "keep_alive": "30m",
    }
    payload.update(extra)
    return payload


@pytest.mark.parametrize("tier,expected", [
    ("general", "openai_compatible"),
    ("code", "sonder_inference"),
    ("fast", "ollama"),
    # The resolved ``sonder`` label is the local Ollama alias (strict mode).
    ("sonder", "ollama"),
    ("model:llama3:8b", "ollama"),
    ("cloud-code", "ollama"),
    ("", "ollama"),
])
def test_provider_for_tier_follows_the_contract_routing_rules(tier, expected):
    assert bridge.provider_for_tier(tier, BINDINGS) == expected


def test_ollama_payload_converts_to_a_provider_neutral_request():
    request = bridge.model_request_from_ollama_payload(_payload(), tier="general")
    assert request.tier == "general"
    assert request.system == "be brief"
    assert request.prompt == "current question"
    assert request.history == (
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
    )
    # Only the three portable knobs survive; the Ollama model name and the
    # hardware/runtime options never reach another provider.
    assert request.options == {"temperature": 0.3, "num_predict": 64, "num_ctx": 8192}
    assert "model" not in request.options


def test_bridge_forwards_sampling_and_reasoning_controls_for_inference():
    payload = _payload(
        think=False,
        reasoning_budget_tokens=123,
        reasoning_budget_message="keep the answer short",
    )
    payload["options"].update({
        "top_p": 0.95, "top_k": 20, "min_p": 0, "repeat_penalty": 1.1,
        "seed": 7, "stop": ["DONE"],
    })
    request = bridge.model_request_from_ollama_payload(
        payload, tier="code", provider="sonder_inference",
    )
    assert request.options["think"] is False
    assert request.options["top_p"] == 0.95
    assert request.options["top_k"] == 20
    assert request.options["min_p"] == 0.0
    assert request.options["repeat_penalty"] == 1.1
    assert request.options["seed"] == 7
    assert request.options["stop"] == ["DONE"]
    assert request.options["reasoning_budget_tokens"] == 123
    assert request.options["reasoning_budget_message"] == "keep the answer short"


def test_binding_options_are_cloned_and_explicit_think_wins():
    defaults = {"think": False, "top_p": 0.95}
    with bridge.bind_rung("sonder_inference", "code", options=defaults):
        defaults["think"] = True
        request = bridge.model_request_from_ollama_payload(
            _payload(think=True), tier="code", provider="sonder_inference",
        )
        assert request.options["think"] is True
        assert request.options["top_p"] == 0.95
    with bridge.bind_rung("openrouter", "code", options={"think": False, "top_p": 0.1}):
        request = bridge.model_request_from_ollama_payload(
            _payload(), tier="code", provider="openrouter",
        )
        assert "top_p" not in request.options


@pytest.mark.parametrize("extra", [
    {"format": {"type": "object"}},
    {"format": "json"},
    {"think": True},
    {"tools": [{"type": "function", "function": {"name": "x"}}]},
])
def test_ollama_only_features_are_refused(extra):
    with pytest.raises(bridge.UnsupportedProviderFeature):
        bridge.model_request_from_ollama_payload(_payload(**extra), tier="general")


def test_images_and_native_tool_calls_in_messages_are_refused():
    payload = _payload()
    payload["messages"][-1]["images"] = ["aGVsbG8="]
    with pytest.raises(bridge.UnsupportedProviderFeature):
        bridge.model_request_from_ollama_payload(payload, tier="general")


@pytest.mark.parametrize("extra,feature", [
    ({"format": {"type": "object"}}, "format"),
    ({"format": "json"}, "format"),
    ({"think": True}, "think"),
    ({"tools": [{"type": "function", "function": {"name": "x"}}]}, "tools"),
])
def test_ollama_only_feature_predicate_matches_the_refusal(extra, feature):
    found = bridge.ollama_only_feature(_payload(**extra), provider="openrouter")
    assert found is not None and found[0] == feature
    with pytest.raises(bridge.UnsupportedProviderFeature) as caught:
        bridge.model_request_from_ollama_payload(
            _payload(**extra), tier="general", provider="openrouter",
        )
    assert str(caught.value) == found[1]


def test_ollama_only_feature_predicate_covers_messages_and_thinking_providers():
    payload = _payload()
    payload["messages"][-1]["images"] = ["aGVsbG8="]
    assert bridge.ollama_only_feature(payload)[0] == "images"
    payload = _payload()
    payload["messages"][-2]["tool_calls"] = [{"function": {"name": "x"}}]
    assert bridge.ollama_only_feature(payload)[0] == "tool_calls"
    # Inference carries think itself; think=False is never a feature.
    assert bridge.ollama_only_feature(_payload(think=True), provider="sonder_inference") is None
    assert bridge.ollama_only_feature(_payload(think=False)) is None
    assert bridge.ollama_only_feature(_payload()) is None


def test_think_false_is_not_a_feature_request():
    request = bridge.model_request_from_ollama_payload(_payload(think=False), tier="general")
    assert request.prompt == "current question"


def test_payload_must_end_with_a_user_message():
    payload = _payload()
    payload["messages"].append({"role": "assistant", "content": "dangling"})
    with pytest.raises(InvalidInput):
        bridge.model_request_from_ollama_payload(payload, tier="general")


def test_response_is_shaped_as_the_ollama_reply_legacy_callers_read():
    shaped = bridge.ollama_shape(ModelResponse(
        text="hello", model="served-model", tier="general", tokens_in=11, tokens_out=3,
    ))
    assert shaped == {
        "model": "served-model",
        "message": {"role": "assistant", "content": "hello"},
        "done": True,
        "prompt_eval_count": 11,
        "eval_count": 3,
    }


def test_response_without_usage_omits_counts():
    shaped = bridge.ollama_shape(ModelResponse(text="hi", model="m", tier="general"))
    assert "prompt_eval_count" not in shaped and "eval_count" not in shaped


@pytest.mark.parametrize("reason", ["length", "stop"])
def test_response_shape_preserves_provider_finish_reason(reason):
    response = SimpleNamespace(
        model="m", text="", tokens_in=None, tokens_out=None,
        telemetry=None, finish_reason=reason,
    )
    assert bridge.ollama_shape(response)["done_reason"] == reason


@pytest.mark.parametrize("error,kind,status", [
    (DependencyUnavailable("connection refused"), bridge.PROVIDER_UNAVAILABLE_KIND, 503),
    (bridge.UnsupportedProviderFeature("no schemas"), bridge.UNSUPPORTED_FEATURE_KIND, 400),
    (InvalidInput("bad"), "configuration", 400),
    (Forbidden("remote endpoint"), "configuration", 403),
    (DeadlineExceeded("late"), "timeout", None),
    (Cancelled("stop"), "cancelled", None),
    (CapacityExceeded("busy"), "request", 429),
    (InternalFailure("oops"), "request", 502),
])
def test_domain_errors_classify_to_transport_failures(error, kind, status):
    failure = bridge.classify_failure(error, provider="openai_compatible")
    assert (failure.kind, failure.status) == (kind, status)


def test_provider_unavailable_names_the_provider_and_never_escalates():
    failure = bridge.classify_failure(
        DependencyUnavailable("connect refused at http://127.0.0.1:11437"),
        provider="sonder_inference",
    )
    assert "sonder_inference" in failure.detail
    error = SimpleNamespace(kind=failure.kind, cloud=False)
    assert tier_escalation.failure_reason(error=error) is None
    unsupported = SimpleNamespace(kind=bridge.UNSUPPORTED_FEATURE_KIND, cloud=False)
    assert tier_escalation.failure_reason(error=unsupported) is None


def test_rung_binding_is_scoped_and_ollama_binds_nothing():
    assert bridge.active_rung() is None
    with bridge.bind_rung("openai_compatible", "general") as binding:
        assert bridge.active_rung() is binding
        assert (binding.provider, binding.tier) == ("openai_compatible", "general")
        with bridge.suspend_rung():
            assert bridge.active_rung() is None
        assert bridge.active_rung() is binding
        with bridge.bind_rung("ollama", "fast") as ollama:
            assert ollama is None and bridge.active_rung() is None
    assert bridge.active_rung() is None


def test_gateway_call_runs_with_the_rung_suspended_and_records_the_served_model():
    observed = {}

    class Gateway:
        def generate(self, request, context):
            observed["rung_during_call"] = bridge.active_rung()
            observed["request"] = request
            observed["context"] = context
            return ModelResponse(text="answer", model="provider-model", tier=request.tier,
                                 tokens_in=5, tokens_out=2)

    context = local_owner_context(correlation_id="turn-1", source="http")
    with bridge.bind_rung("openai_compatible", "general") as binding:
        shaped, response = bridge.generate_via_gateway(
            Gateway(), _payload(), tier="general", context=context,
        )
    assert observed["rung_during_call"] is None
    assert observed["context"] is context
    assert observed["request"].tier == "general"
    assert shaped["message"]["content"] == "answer"
    assert shaped["prompt_eval_count"] == 5
    assert response.model == "provider-model"
    assert binding.served_model == "provider-model"


def test_gateway_returning_a_non_response_fails_closed():
    class Gateway:
        def generate(self, request, context):
            return {"text": "not a ModelResponse"}

    with pytest.raises(DependencyUnavailable):
        bridge.generate_via_gateway(
            Gateway(), _payload(), tier="general",
            context=local_owner_context(correlation_id="turn-2"),
        )


def test_degradations_are_collected_only_inside_a_turn_scope():
    assert bridge.record_degradation("memory_recall_embeddings") is False
    with bridge.degradation_scope() as notes:
        assert bridge.record_degradation("memory_recall_embeddings") is True
        bridge.record_degradation("memory_recall_embeddings")
    assert notes == ["memory_recall_embeddings"]


def test_gateway_finish_metadata_shapes_real_model_response():
    class Gateway:
        last_response_meta = {"finish_reason": "length"}
        def generate(self, request, context):
            return ModelResponse(text="{", model="fake", tier=request.tier)
    with bridge.bind_rung("sonder_inference", "code"):
        shaped, _ = bridge.generate_via_gateway(
            Gateway(), _payload(), tier="code",
            context=local_owner_context(correlation_id="length"),
        )
    assert shaped["done_reason"] == "length"


def test_stop_sequences_are_not_silently_dropped_at_bridge():
    stops = ["a", "b", "c", "d", "e"]
    payload = _payload()
    payload["options"] = {"stop": stops, "seed": 0, "top_k": 0}
    request = bridge.model_request_from_ollama_payload(payload, tier="code", provider="sonder_inference")
    assert request.options == {"stop": stops, "seed": 0, "top_k": 0}


@pytest.mark.parametrize("content", [None, "", "  \n"])
def test_empty_length_response_retains_transport_repair_signal(content):
    from sonder_runtime.adapters.inference.openai_compat_gateway import OpenAICompatibleGateway
    from sonder_runtime.adapters.model_transport import ModelCallError
    with pytest.raises(ModelCallError) as raised:
        OpenAICompatibleGateway._extract_text({"choices": [{"finish_reason": "length", "message": {"content": content}}]})
    assert raised.value.kind == "empty_response"
    assert '"done_reason": "length"' in raised.value.detail


def test_malformed_length_content_is_still_a_protocol_error():
    from sonder_runtime.adapters.inference.openai_compat_gateway import OpenAICompatibleGateway
    with pytest.raises(DependencyUnavailable):
        OpenAICompatibleGateway._extract_text({"choices": [{"finish_reason": "length", "message": {"content": {"bad": "shape"}}}]})
