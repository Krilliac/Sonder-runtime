"""Wire and helper contracts for provider-bound generation entrypoints."""
from __future__ import annotations

import json
from types import SimpleNamespace
import pytest

from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.platform.runtime_threads import Thread
from sonder_runtime.application.context import bind_operation_context, local_owner_context


def test_tier_wrapper_ollama_payload_usage_and_override_match_legacy(monkeypatch):
    import server

    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform("ollama"))
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    captured = []

    def post(_path, payload, **kwargs):
        captured.append((json.dumps(payload, sort_keys=True, separators=(",", ":")), kwargs))
        return (
            {"message": {"content": "  answer with whitespace \n"},
             "prompt_eval_count": 2, "eval_count": 3},
            1,
        )

    monkeypatch.setattr(server, "_post_model", post)
    legacy = server._make_generate(
        "same-model", "system", .2, 111, 2048, cloud=False,
        timeout=5,
    )
    legacy.num_predict_override = 77
    legacy_text = legacy("prompt", [{"role": "user", "content": "history"}])
    legacy_wire = captured[-1]
    legacy_usage = dict(legacy.last_usage)

    tiered = server._make_tier_generate(
        "code", "same-model", "system", .2, 111, 2048,
        cloud=False, timeout=5,
    )
    tiered.num_predict_override = 77
    tiered_text = tiered("prompt", [{"role": "user", "content": "history"}])
    tiered_wire = captured[-1]

    assert tiered_text == legacy_text == "  answer with whitespace \n"
    assert tiered_wire[0] == legacy_wire[0]
    assert tiered_wire[1].get("model") == legacy_wire[1].get("model")
    assert dict(tiered.last_usage) == legacy_usage
    assert json.loads(tiered_wire[0])["options"]["num_predict"] == 77


def test_offload_learn_false_uses_bound_gateway_without_ollama_probe(monkeypatch):
    import server

    class Gateway:
        def __init__(self):
            self.requests = []

        def generate(self, request, _context):
            self.requests.append(request)
            return ModelResponse("offloaded", "fake-si", request.tier, tokens_in=2, tokens_out=1)

    gateway = Gateway()
    graph = SimpleNamespace(
        provider_bindings=ProviderBindings.uniform("sonder_inference"),
        model_gateway=gateway,
    )
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setitem(server.TIERS, "fast", "fake-model")
    monkeypatch.setattr(server, "_post_model", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("bound offload called Ollama")
    ))
    monkeypatch.setattr(server, "_auto_model_context", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("bound offload probed Ollama context")
    ))
    assert server._offload_impl("hello", tier="fast", learn=False, num_ctx=4096) == "offloaded"
    assert len(gateway.requests) == 1


def test_offload_learning_passes_bound_generator_to_orchestrator(monkeypatch):
    import server

    class Gateway:
        def generate(self, request, _context):
            return ModelResponse("learned", "fake-si", request.tier)

    graph = SimpleNamespace(
        provider_bindings=ProviderBindings.uniform("sonder_inference"),
        model_gateway=Gateway(),
    )
    seen = {}
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setitem(server.TIERS, "fast", "fake-model")
    class Connection:
        def close(self):
            pass

    monkeypatch.setattr(server, "_open_db", lambda: Connection())
    monkeypatch.setattr(server, "resolve_sonder_model", lambda *_args, **_kwargs: (_ for _ in ()).throw(
        AssertionError("learning path resolved a model outside the tier binding")
    ))
    monkeypatch.setattr(server, "_post_model", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("learning path called Ollama")
    ))

    def run_with_learning(_conn, _prompt, _tier, generator, **_kwargs):
        seen["text"] = generator("learning prompt")
        return seen["text"], "interaction-id"

    monkeypatch.setattr(server.orchestrator, "run_with_learning", run_with_learning)
    output = server._offload_impl("hello", tier="fast", learn=True, num_ctx=4096)
    assert output.endswith("[interaction_id: interaction-id]"), repr(output)
    assert seen["text"] == "learned"


def test_negative_claim_review_uses_resolved_tier_binding(monkeypatch):
    import server

    class Gateway:
        def __init__(self):
            self.requests = []

        def generate(self, request, _context):
            self.requests.append(request)
            return ModelResponse(
                '{"decision":"accept","reason":"evidence",'
                '"tool":"","args":{}}', "fake-si", request.tier,
            )

    gateway = Gateway()
    graph = SimpleNamespace(
        provider_bindings=ProviderBindings.uniform("sonder_inference"),
        model_gateway=gateway,
    )
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "system")
    monkeypatch.setattr(server._prompts, "render", lambda *args, **kwargs: "review")
    monkeypatch.setattr(server, "_agent_exact_negative_action", lambda *args, **kwargs: None)
    monkeypatch.setattr(server, "_agent_claim_review_vocabulary", lambda *args, **kwargs: ())
    monkeypatch.setattr(server, "_post_model", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("negative claim review called Ollama")
    ))
    result = server._agent_negative_claim_review(
        "check whether the file exists", "No such file exists", [],
        "fake-model", False, tier="code",
    )
    assert result["decision"] == "accept"
    assert len(gateway.requests) == 1
    assert gateway.requests[0].tier == "code"


def test_cross_thread_hosted_consent_is_checked_at_invocation(monkeypatch):
    import server

    graph = SimpleNamespace(
        provider_bindings=ProviderBindings.uniform("openrouter"),
        model_gateway=object(),
    )
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_cloud_allowed_policy", lambda _env: True)
    monkeypatch.setattr(server, "_post_model", lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError("hosted helper reached Ollama")
    ))
    origin = local_owner_context(correlation_id="private-task", cloud_allowed=False)
    with bind_operation_context(origin):
        generator = server._make_tier_generate(
            "fast", "model", "system", 0.1, 20, 0,
            cloud=False, timeout=5,
        )
    errors = []

    def invoke():
        try:
            generator("private prompt")
        except Exception as exc:
            errors.append(exc)

    worker = Thread(target=invoke)
    worker.start()
    worker.join(5)
    assert len(errors) == 1
    assert isinstance(errors[0], ModelCallError)
    assert errors[0].status == 403


@pytest.mark.parametrize("provider", ["sonder_inference", "openai_compatible", "openrouter"])
def test_agent_turn_routes_through_provider_and_keeps_hosted_system_private(monkeypatch, provider):
    import server

    calls = []

    class Gateway:
        def generate(self, request, context):
            calls.append((request, context))
            return ModelResponse('{"final":"Hello there."}', "bound-model", request.tier)

    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform(provider), model_gateway=Gateway())
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_application", lambda: graph)
    monkeypatch.setattr(server, "_serve_target", lambda *_args: ("same-model", False, False, "code"))
    monkeypatch.setattr(server, "_cloud_allowed_policy", lambda _env: True)
    monkeypatch.setattr(server, "_agent_tool_help", lambda **_kwargs: "")
    monkeypatch.setattr(server, "_build_system", lambda *args, **kwargs: "private-host-profile")
    monkeypatch.setattr(server, "_post_model", lambda *_args, **_kwargs: pytest.fail("agent sent to Ollama"))
    monkeypatch.setattr(server, "_auto_model_context", lambda *_args: pytest.fail("agent probed Ollama"))
    result = server._agent_impl(
        "Say hello", tier="code", max_steps=1, allow_web=False,
        auto_checklist=False, require_file_evidence=False, system="request-system",
    )
    assert "Hello there." in result
    assert len(calls) == 1
    assert calls[0][0].tier == "code"
    assert calls[0][0].options["num_predict"] == {
        "sonder_inference": 4096, "openai_compatible": 1200,
        "openrouter": server._CLOUD_AGENT_NUM_PREDICT,
    }[provider]
    if provider == "openrouter":
        assert calls[0][0].system == "request-system"


@pytest.mark.parametrize("fallback_enabled,error_kind,expected", [
    (False, "unreachable", "provider_unavailable"),
    (True, "unreachable", "fallback"),
    (True, "timeout", "timeout"),
])
def test_helper_uses_configured_dispatch_fallback_only_before_execution(monkeypatch, fallback_enabled, error_kind, expected):
    import server
    from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceUnreachable
    from sonder_runtime.adapters.provider_dispatch.fallback import PreSendFallbackGateway
    from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
    from sonder_runtime.domain.common.errors import DeadlineExceeded

    fallback_calls = []

    class Primary:
        def generate(self, _request, _context):
            if error_kind == "unreachable":
                raise SonderInferenceUnreachable("connection refused before send")
            raise DeadlineExceeded("primary may have executed")

    class Fallback:
        def generate(self, request, context):
            fallback_calls.append((request, context))
            return ModelResponse("fallback", "ollama-model", request.tier)

    base = ProviderBindings.uniform("sonder_inference")
    bindings = ProviderBindings(base.default_generation_provider, base.tier_providers, base.embedding_provider,
                                {"sonder_inference": "ollama"} if fallback_enabled else {})
    primary = PreSendFallbackGateway(Primary(), fallback=Fallback()) if fallback_enabled else Primary()
    gateway = ProviderDispatchGateway(
        providers={"sonder_inference": primary}, tier_providers=bindings.tier_providers,
        default_generation_provider="sonder_inference", embedding_provider="sonder_inference",
    )
    graph = SimpleNamespace(provider_bindings=bindings, model_gateway=gateway)
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_application", lambda: graph)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_post_model", lambda *_args, **_kwargs: pytest.fail("unconfigured legacy fallback"))
    if expected == "fallback":
        assert server._generate_text("plan", tier="code") == "fallback"
        assert len(fallback_calls) == 1
        assert fallback_calls[0][1].cloud_allowed is False
        assert fallback_calls[0][1].remote_ollama_allowed is False
    else:
        with pytest.raises(ModelCallError) as error:
            server._generate_text("plan", tier="code")
        assert error.value.kind == expected
        assert fallback_calls == []


def test_helper_does_not_claim_or_write_the_enclosing_chat_stream(monkeypatch):
    import server
    from sonder_runtime.application.chat import stream_sink

    class Gateway:
        def generate(self, request, _context):
            assert stream_sink.call_stream() is None
            return ModelResponse("private plan", "provider", request.tier)

    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform("sonder_inference"), model_gateway=Gateway())
    monkeypatch.setattr(server, "_APP_GRAPH", graph)
    monkeypatch.setattr(server, "_application", lambda: graph)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    stream = stream_sink.LiveTurnStream(lambda _text: pytest.fail("helper text leaked"))
    with stream_sink.armed(stream):
        assert server._generate_text("plan", tier="code") == "private plan"
        assert not stream.claimed


def _generation_host(graph, wire):
    """Load the real small server entrypoints without booting its global stores.

    Keep transport/capture side effects fake; execute the actual generator and
    its agent construction statements to catch wiring regressions as well as
    the pure policies. Normal server fixtures still cover the complete host.
    """
    import ast
    from pathlib import Path
    import time
    import os
    from sonder_runtime.adapters import agent_generation_budget, tier_generation
    from sonder_runtime.application.chat import provider_bridge

    tree = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}

    def chat(payload, **_kwargs):
        wire.append(json.dumps(payload, sort_keys=True, separators=(",", ":")))
        binding = provider_bridge.active_rung()
        if binding is not None:
            out, _response = provider_bridge.generate_via_gateway(
                graph.model_gateway, payload, tier=binding.tier,
                context=local_owner_context(correlation_id="test", source="test"),
            )
        else:
            out = {"message": {"content": '{"final":"done"}'}, "prompt_eval_count": 2, "eval_count": 3}
        return out, out["message"]["content"]

    namespace = {
        "os": os, "time": time, "_provider_bridge": provider_bridge,
        "_tier_generation": tier_generation, "_APP_GRAPH": graph,
        "_agent_generation_budget": agent_generation_budget,
        "_LOCAL_AGENT_NUM_PREDICT": 1200,
        "_CLOUD_AGENT_NUM_PREDICT": 16384, "_CLOUD_AGENT_OUTPUT_BUDGET": 65536,
        "_cloud_allowed_policy": lambda _: True, "_ollama_endpoint_is_local": lambda: True,
        "_is_cloud_model_name": lambda _: False, "_auto_model_context": lambda _: 4096,
        "_platform_local_model_options": lambda temperature, num_predict, num_ctx, **kw: {
            "temperature": temperature, "num_predict": num_predict, "num_ctx": num_ctx,
        },
        "context_policy": SimpleNamespace(native=False), "_keep_alive_for": lambda _: "5m",
        "_chat_request": chat, "ModelCallError": ModelCallError,
        "_model_usage_count": lambda x: x, "_model_usage_source": lambda *x: "measured",
        "activity_tracker": SimpleNamespace(record_model_call=lambda **kw: None),
    }
    module = ast.Module(body=[funcs["_make_generate"], funcs["_make_tier_generate"]], type_ignores=[])
    exec(compile(module, "server.py", "exec"), namespace)

    def construct_agent(provider):
        body = funcs["_agent_turn"].body
        start = next(i for i, n in enumerate(body) if isinstance(n, ast.Assign)
                     and any(isinstance(t, ast.Name) and t.id == "agent_num_predict" for t in n.targets))
        end = next(i for i in range(start, len(body)) if isinstance(body[i], ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == "gen" for t in body[i].targets))
        namespace.update(provider=provider, cloud=False, pre_model_context=None, model="model",
                         tier_label="code", system="system", cancel_check=None)
        exec(compile(ast.Module(body=body[start:end + 1], type_ignores=[]), "server.py", "exec"), namespace)
        return namespace["gen"]

    return namespace, construct_agent


@pytest.mark.parametrize("provider,expected", [("sonder_inference", 4096), ("ollama", 1200)])
def test_isolated_agent_construction_sets_real_cap_and_preserves_ollama_bytes(monkeypatch, provider, expected):
    requests = []
    class Gateway:
        def generate(self, request, _context):
            requests.append(request)
            return ModelResponse('{"final":"done"}', "fake", request.tier, tokens_in=2, tokens_out=3)

    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform(provider), model_gateway=Gateway())
    wire = []
    namespace, construct = _generation_host(graph, wire)
    monkeypatch.delenv("SONDER_AGENT_NUM_PREDICT", raising=False)
    monkeypatch.delenv("SONDER_AGENT_TEMPERATURE", raising=False)
    gen = construct(provider if provider != "ollama" else None)
    assert gen("task") == '{"final":"done"}'
    assert json.loads(wire[-1])["options"]["num_predict"] == expected
    if provider == "sonder_inference":
        assert requests[0].options["num_predict"] == 4096
    else:
        original = wire[-1]
        monkeypatch.setenv("SONDER_AGENT_TEMPERATURE", "0.6")
        monkeypatch.setenv("SONDER_AGENT_SAMPLING", "0.95,20,0")
        monkeypatch.setenv("SONDER_AGENT_DECISION_THINKING", "off")
        construct(None)("task")
        assert wire[-1] == original
        namespace["_make_generate"]("model", "system", .1, 1200, 0)("task")
        assert wire[-1] == original


def test_isolated_planner_json_budget_and_thinking_are_provider_scoped(monkeypatch):
    import ast
    from pathlib import Path
    from sonder_runtime.domain.agents.decision_parsing import extract_agent_json

    requests = []
    class Gateway:
        def health(self):
            return SimpleNamespace(document={"sonder": {"features": ["thinking"]}})
        def generate(self, request, _context):
            requests.append(request)
            return ModelResponse('{"tasks":[]}', "fake", request.tier, tokens_in=2, tokens_out=3)
    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform("sonder_inference"), model_gateway=Gateway())
    namespace, _construct = _generation_host(graph, [])
    namespace.update(
        autopilot_controller=SimpleNamespace(normalize_tier=lambda x: x, LOCAL_TIERS={"code"}),
        _serve_target=lambda *x: ("model", False, False, "code"),
        _bridge_provider_for_tier=lambda _: "sonder_inference",
        _build_system=lambda *a, **kw: "system", _prompts=SimpleNamespace(render=lambda *a, **kw: "system"),
        _extract_agent_json=extract_agent_json,
    )
    source = ast.parse((Path(__file__).parents[1] / "server.py").read_text(encoding="utf-8"))
    function = next(n for n in source.body if isinstance(n, ast.FunctionDef) and n.name == "_autopilot_json_model")
    exec(compile(ast.Module(body=[function], type_ignores=[]), "server.py", "exec"), namespace)
    monkeypatch.setenv("SONDER_AUTOPILOT_JSON_NUM_PREDICT", "5000")
    monkeypatch.setenv("SONDER_AUTOPILOT_JSON_THINK", "auto")
    assert namespace["_autopilot_json_model"]({}, "planner", "plan", lambda _: None) == {"tasks": []}
    assert requests[0].options["num_predict"] == 5000
    assert requests[0].options["think"] is False
