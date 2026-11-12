"""Provider binding coverage for legacy tier generators.

These tests stay below the server composition root: the production wrapper is
intentionally injectable so autopilot and agent paths can be verified without
starting HTTP, creating a live application, or probing Ollama metadata.
"""
from __future__ import annotations

from types import SimpleNamespace
import threading

import pytest

from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.adapters.tier_generation import local_only, make_generate
from sonder_runtime.application.chat import provider_bridge
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.platform.runtime_threads import Thread


class _Gateway:
    def __init__(self):
        self.requests = []
        self.lock = threading.Lock()

    def generate(self, request, _context):
        with self.lock:
            self.requests.append(request)
        return ModelResponse(
            text='{"tasks": [{"id": "t1"}]}', model="fake-si", tier=request.tier,
            tokens_in=7, tokens_out=3,
        )


def _graph(*, fast="sonder_inference", code="ollama", fallback=None):
    bindings = ProviderBindings(
        "ollama",
        {"fast": fast, "general": "ollama", "code": code,
         "reasoning": "ollama", "vision": "ollama"},
        "ollama",
        {"sonder_inference": "ollama"} if fallback else {},
    )
    return SimpleNamespace(provider_bindings=bindings, model_gateway=_Gateway())


def _factory(graph, calls):
    def factory(*args, **kwargs):
        calls.append((args, dict(kwargs), provider_bridge.active_rung()))

        class Raw:
            num_predict_override = None
            last_usage = {"tokens_in": 7, "tokens_out": 3}
            last_response_meta = {}

            def __call__(self, prompt):
                binding = provider_bridge.active_rung()
                if binding is not None:
                    payload = {
                        "messages": [{"role": "user", "content": prompt}],
                        "options": {"temperature": args[2], "num_predict": args[3]},
                    }
                    from sonder_runtime.application.context import local_owner_context
                    context = local_owner_context(
                        correlation_id="test", source="test", timeout_seconds=5,
                        cloud_allowed=True, remote_ollama_allowed=False,
                    )
                    from sonder_runtime.application.chat.provider_bridge import generate_via_gateway
                    shaped, _response = generate_via_gateway(
                        graph.model_gateway, payload, tier=binding.tier,
                        context=context,
                    )
                    return shaped["message"]["content"]
                return "ollama-parity"

        return Raw()
    return factory


def test_six_concurrent_planning_calls_use_bound_sonder_inference_gateway_only():
    graph = _graph()
    calls = []
    factory = _factory(graph, calls)
    results = []
    errors = []

    def invoke():
        try:
            generator = make_generate(
                factory, "fast", ("model", "system", .05, 1800, 0),
                {"cloud": False, "timeout": 5}, graph=graph,
                consent=lambda: (True, False),
            )
            results.append(generator("plan this"))
        except Exception as exc:  # retain all worker failures for assertion
            errors.append(exc)

    threads = [Thread(target=invoke) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert not errors
    assert len(results) == 6
    assert len(graph.model_gateway.requests) == 6
    assert provider_bridge.active_rung() is None
    assert all(call[2] is not None and call[2].provider == "sonder_inference" for call in calls)


def test_same_model_follows_tier_binding_and_ollama_call_preserves_wire_options():
    graph = _graph(fast="sonder_inference", code="ollama")
    calls = []
    factory = _factory(graph, calls)
    fast = make_generate(
        factory, "fast", ("same-model", "system", .1, 321, 4096),
        {"cloud": False}, graph=graph, consent=lambda: (True, False),
    )
    code = make_generate(
        factory, "code", ("same-model", "system", .2, 654, 2048),
        {"cloud": False}, graph=graph, consent=lambda: (True, False),
    )
    assert fast("through gateway") == '{"tasks": [{"id": "t1"}]}'
    assert code("through ollama") == "ollama-parity"
    assert len(graph.model_gateway.requests) == 1
    assert calls[0][1]["cloud"] is False
    assert calls[1][0][2:] == (.2, 654, 2048)


def test_openrouter_requires_operation_cloud_consent():
    graph = _graph(fast="openrouter")
    calls = []
    generator = make_generate(
        _factory(graph, calls), "fast", ("model", "system", .1, 10, 0),
        {"cloud": False}, graph=graph, consent=lambda: (False, False),
    )
    with pytest.raises(ModelCallError, match="requires cloud consent"):
        generator("secret")
    assert not graph.model_gateway.requests


def test_local_only_suppresses_cloud_consent_for_explicit_local_helpers():
    graph = _graph(fast="openrouter")
    generator = make_generate(
        _factory(graph, []), "fast", ("model", "system", .1, 10, 0),
        {"cloud": True}, graph=graph, consent=lambda: (True, False),
    )
    with local_only(), pytest.raises(ModelCallError, match="requires cloud consent"):
        generator("must stay local")


@pytest.mark.parametrize("ambient_timeout, drained", [(0.001, False), (60, True)])
def test_ambient_http_deadline_and_drain_token_do_not_bound_a_helper_model_step(
    ambient_timeout, drained,
):
    """A helper call inherits the chat bridge's bounds: its own timeout and cancel_check.

    The HTTP context carries a 30 s admission deadline and the lifecycle drain
    token.  Neither may cut short an agent step on a bridged tier, exactly as
    neither does on the Ollama path (see ``BridgeCancellation``).
    """
    import time

    from sonder_runtime.adapters import legacy_chat_bridge
    from sonder_runtime.application.context import bind_operation_context, local_owner_context

    class Drain:
        cancelled = drained

        def wait(self, timeout=None):
            return drained

    seen = []

    def factory(*_args, **_kwargs):
        def raw(_prompt):
            context = legacy_chat_bridge.operation_context(
                30, None, cloud_allowed=False, remote_ollama_allowed=False,
            )
            seen.append((context.remaining_seconds, context.cancellation.cancelled))
            return "answer"
        return raw

    ambient = local_owner_context(
        correlation_id="http-turn", source="http", timeout_seconds=ambient_timeout,
        cancellation=Drain(),
    )
    time.sleep(0.02)
    with bind_operation_context(ambient):
        generator = make_generate(
            factory, "fast", ("model", "system", .1, 10, 0), {"timeout": 30},
            graph=_graph(fast="sonder_inference"), consent=lambda: (False, False),
        )
        assert generator("hello") == "answer"
    remaining, cancelled = seen[0]
    assert remaining > 25
    assert cancelled is False


def test_helper_cancel_check_still_cancels_the_bridged_step():
    from sonder_runtime.adapters import legacy_chat_bridge

    seen = []

    def factory(*_args, **_kwargs):
        def raw(_prompt):
            context = legacy_chat_bridge.operation_context(
                30, None, cloud_allowed=False, remote_ollama_allowed=False,
            )
            seen.append(context.cancellation.cancelled)
            return "answer"
        return raw

    flag = {"stop": False}
    generator = make_generate(
        factory, "fast", ("model", "system", .1, 10, 0),
        {"timeout": 30, "cancel_check": lambda: flag["stop"]},
        graph=_graph(fast="sonder_inference"), consent=lambda: (False, False),
    )
    generator("first")
    flag["stop"] = True
    generator("second")
    assert seen == [False, True]


@pytest.mark.parametrize("provider,expected", [("sonder_inference", 4096), ("ollama", 1200), ("openrouter", 1200)])
def test_decision_output_budget_is_provider_scoped(monkeypatch, provider, expected):
    from sonder_runtime.adapters.agent_generation_budget import decision_num_predict

    monkeypatch.delenv("SONDER_AGENT_NUM_PREDICT", raising=False)
    assert decision_num_predict(provider, False, True) == expected


@pytest.mark.parametrize("raw,expected", [("2048", 2048), ("9000", 8192), ("", 4096), ("bad", 4096), ("0", 4096), ("-1", 4096)])
def test_bridged_budget_env_is_bounded(monkeypatch, raw, expected):
    from sonder_runtime.adapters.agent_generation_budget import decision_num_predict, json_num_predict

    monkeypatch.setenv("SONDER_AGENT_NUM_PREDICT", raw)
    monkeypatch.setenv("SONDER_AUTOPILOT_JSON_NUM_PREDICT", raw)
    assert decision_num_predict("sonder_inference", False, True) == expected
    assert json_num_predict("sonder_inference", False, True) == expected
    assert decision_num_predict("sonder_inference", True, True) == 1200
    assert json_num_predict("ollama", False, False) == 1800


@pytest.mark.parametrize("provider", ["sonder_inference", "ollama", "openrouter"])
def test_agent_sampling_env_only_changes_thinking_provider(monkeypatch, provider):
    monkeypatch.setenv("SONDER_AGENT_SAMPLING", "0.95,20,0")
    monkeypatch.setenv("SONDER_AGENT_TEMPERATURE", "0.6")
    calls = []
    graph = _graph(fast=provider)
    generator = make_generate(
        _factory(graph, calls), "fast", ("model", "system", .1, 1200, 4096),
        {"cloud": False, "generation_kind": "decision"}, graph=graph,
        consent=lambda: (True, False),
    )
    generator("decide")
    assert "generation_kind" not in calls[0][1]
    if provider == "sonder_inference":
        assert calls[0][0][2] == .6
        options = graph.model_gateway.requests[0].options
        assert {k: options[k] for k in ("top_p", "top_k", "min_p")} == {"top_p": .95, "top_k": 20, "min_p": 0}
    else:
        assert calls[0][0][2] == .1
        if graph.model_gateway.requests:
            assert not {"top_p", "top_k", "min_p"} & graph.model_gateway.requests[0].options.keys()


@pytest.mark.parametrize("kind,mode,advertised,expected", [
    ("decision", "off", True, False), ("decision", "off", False, None),
    ("decision", "on", True, True), ("decision", "on", False, None),
    ("decision", "auto", True, None), ("decision", "auto", False, None),
    ("json", "auto", True, False), ("json", "auto", False, None),
    ("json", "on", True, True), ("json", "off", True, False),
])
def test_agent_thinking_requires_advertised_health_and_is_frozen(monkeypatch, kind, mode, advertised, expected):
    env = "SONDER_AGENT_DECISION_THINKING" if kind == "decision" else "SONDER_AUTOPILOT_JSON_THINK"
    monkeypatch.setenv(env, mode)
    graph = _graph()
    health_calls = []
    def health():
        health_calls.append(True)
        return SimpleNamespace(document={"sonder": {"features": ["thinking"] if advertised else []}})
    graph.model_gateway.health = health
    generator = make_generate(
        _factory(graph, []), "fast", ("model", "system", .1, 1200, 4096),
        {"generation_kind": kind}, graph=graph, consent=lambda: (True, False),
    )
    monkeypatch.setenv(env, "on" if mode == "off" else "off")
    generator("first")
    generator("second")
    assert len(health_calls) <= 1
    for request in graph.model_gateway.requests:
        if expected is None:
            assert "think" not in request.options
        else:
            assert request.options["think"] is expected


def test_ordinary_helper_does_not_read_agent_sampling_env_or_health(monkeypatch):
    monkeypatch.setenv("SONDER_AGENT_SAMPLING", "broken")
    monkeypatch.setenv("SONDER_AGENT_TEMPERATURE", "broken")
    graph = _graph()
    graph.model_gateway.health = lambda: pytest.fail("ordinary helper probed health")
    calls = []
    generator = make_generate(_factory(graph, calls), "fast", ("model", "system", .2, 100, 0), {}, graph=graph, consent=lambda: (True, False))
    generator("plain")
    # think is unset, so the bridge leaves a possibly-thinking Inference model the
    # local thinking headroom (provider_bridge.generate_via_gateway).
    assert graph.model_gateway.requests[0].options == {"temperature": .2, "num_predict": 4096}


@pytest.mark.parametrize("name,value", [
    ("SONDER_AGENT_TEMPERATURE", "nan"), ("SONDER_AGENT_TEMPERATURE", "inf"),
    ("SONDER_AGENT_TEMPERATURE", "-1"), ("SONDER_AGENT_TEMPERATURE", "bad"),
    ("SONDER_AGENT_SAMPLING", "0.95,20"), ("SONDER_AGENT_SAMPLING", "0.95,20.5,0"),
    ("SONDER_AGENT_SAMPLING", "1.1,20,0"), ("SONDER_AGENT_SAMPLING", "0.95,-1,0"),
    ("SONDER_AGENT_DECISION_THINKING", "sometimes"),
])
def test_invalid_agent_policy_is_rejected_before_model_call(monkeypatch, name, value):
    from sonder_runtime.domain.common.errors import InvalidInput
    monkeypatch.setenv(name, value)
    graph = _graph()
    graph.model_gateway.health = lambda: pytest.fail("invalid policy probed health")
    with pytest.raises(InvalidInput):
        make_generate(_factory(graph, []), "fast", ("model", "system", .1, 4096, 0),
                      {"generation_kind": "decision"}, graph=graph, consent=lambda: (True, False))
    assert not graph.model_gateway.requests


def test_thinking_health_uses_selected_primary_in_mixed_fallback_graph(monkeypatch):
    from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
    from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway

    monkeypatch.setenv("SONDER_AGENT_DECISION_THINKING", "off")
    primary = _Gateway()
    primary.health = lambda: SimpleNamespace(document={"sonder": {"features": ["thinking"]}})
    fallback = _Gateway()
    fallback.health = lambda: pytest.fail("fallback must not be probed")
    graph = _graph()
    graph.model_gateway = ProviderDispatchGateway(
        providers={"sonder_inference": PreSendFallbackGateway(primary, fallback=fallback), "ollama": fallback},
        tier_providers=graph.provider_bindings.tier_providers,
        default_generation_provider="ollama", embedding_provider="ollama",
    )
    generator = make_generate(_factory(graph, []), "fast", ("model", "system", .1, 4096, 0),
                              {"generation_kind": "decision"}, graph=graph, consent=lambda: (True, False))
    generator("decide")
    assert primary.requests[0].options["think"] is False
    assert not fallback.requests


def test_constructed_sampling_does_not_change_when_environment_changes(monkeypatch):
    monkeypatch.setenv("SONDER_AGENT_TEMPERATURE", "0.6")
    monkeypatch.setenv("SONDER_AGENT_SAMPLING", "0.95,20,0")
    graph = _graph()
    generator = make_generate(_factory(graph, []), "fast", ("model", "system", .1, 4096, 0),
                              {"generation_kind": "decision"}, graph=graph, consent=lambda: (True, False))
    monkeypatch.setenv("SONDER_AGENT_TEMPERATURE", "0.7")
    monkeypatch.setenv("SONDER_AGENT_SAMPLING", "0.8,40,0.1")
    generator("first")
    generator("second")
    assert [r.options for r in graph.model_gateway.requests] == [
        {"temperature": .6, "num_predict": 4096, "top_p": .95, "top_k": 20, "min_p": 0},
    ] * 2


@pytest.mark.parametrize("thinking,expected_p", [("on", .95), ("off", .8)])
def test_opted_in_sampling_defaults_use_decision_row_in_real_bridge(monkeypatch, thinking, expected_p):
    monkeypatch.setenv("SONDER_INFERENCE_SAMPLING_DEFAULTS", "1")
    monkeypatch.setenv("SONDER_AGENT_DECISION_THINKING", thinking)
    graph = _graph()
    graph.model_gateway.health = lambda: SimpleNamespace(document={"features": ["thinking"]})
    generator = make_generate(_factory(graph, []), "fast", ("qwen3.8", "system", .1, 4096, 0),
                              {"generation_kind": "decision"}, graph=graph, consent=lambda: (True, False))
    generator("decide")
    assert graph.model_gateway.requests[0].options["top_p"] == expected_p
    assert graph.model_gateway.requests[0].options["top_k"] == 20
    assert graph.model_gateway.requests[0].options["temperature"] == .1
