"""The packaged ``ModelGateway`` path of the tool-lane deny list.

``ToolLaneGateway`` wraps the shared gateway for the callers that call tools
or apply model output; text callers keep the raw gateway and still answer a
named blocked model as chat.  The shared gateway here is a recorder, the model
catalog is a dictionary, and the composition root is built with its three
tool/apply consumers spied on.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.adapters.model_bootstrap import LegacyModelBootstrapAdapter
from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env
from sonder_runtime.adapters.tool_lane_gateway import ServedTierGateway, ToolLaneGateway
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import Forbidden
from tests.support.tool_lane import (
    ALLOWED,
    BLOCKED,
    DENIED,
    DENIED_SMALL,
    HF_ORNITH,
    HF_QWEN,
    UNKNOWN,
    UNKNOWN_OPENAI,
    UNREADABLE,
    RecordingGateway,
    clear_environment,
    operation_context,
    served_refusal,
    write_policy,
)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    before = dict(server.TIERS)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", server._RUNTIME_POLICY)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    server._refresh_runtime_policy(create=False)
    for tier in ("fast", "code", "general"):
        server.TIERS[tier] = ALLOWED
    yield tmp_path / "runtime_policy.json"
    server.TIERS.clear()
    server.TIERS.update(before)


@pytest.fixture
def catalog(monkeypatch):
    names = {DENIED: DENIED, DENIED_SMALL: DENIED_SMALL, HF_QWEN.casefold(): HF_QWEN, ALLOWED: ALLOWED}

    def record(selector):
        key = str(selector or "").strip().casefold()
        return (names[key], {"name": names[key]}) if key in names else None

    monkeypatch.setattr(server, "resolve_discovered_model_record", record)
    monkeypatch.setattr(server, "_fanout_nonchat_reason", lambda record: "")
    return names


def adapter():
    return LegacyModelBootstrapAdapter(server)


def tool_gateway(inner=None):
    """Built after the environment is set: the bindings are read at construction."""
    inner = RecordingGateway() if inner is None else inner
    return inner, ToolLaneGateway(inner, resolve_target=adapter().resolve_target, bindings=provider_bindings_from_env())


def request(tier, **options):
    return ModelRequest("hi", tier=tier, options=options or None)


# ---- refusals ----------------------------------------------------------------

@pytest.mark.parametrize("name", [
    DENIED, "Astra-Cyber-9B", "ASTRA-CYBER-27B:LATEST", "library/astra-cyber-27b",
    "astra-cyber-70b",  # not in the catalog: a free-form selector is still a model name
    "registry.example.com/team/astra-cyber-1b:q4", HF_QWEN, HF_ORNITH,
    "hf.co/mradermacher/Qwen3.8-cyber-RedTeam-Surgical-Abliterated-GGUF",
])
def test_a_denied_explicit_model_is_refused_by_generate_and_resolve_route(catalog, name):
    inner, tool = tool_gateway()
    for call in (tool.generate, tool.resolve_route):
        with pytest.raises(Forbidden) as caught:
            call(request(name), operation_context())
        assert str(caught.value) == BLOCKED % name
    assert inner.calls == [] and inner.routes == []


@pytest.mark.parametrize("tier", ["fast", "code", "general", "sonder", ""])
def test_a_denied_model_named_in_the_options_is_refused_on_any_tier(catalog, tier):
    inner, tool = tool_gateway()
    for call in (tool.generate, tool.resolve_route):
        with pytest.raises(Forbidden) as caught:
            call(request(tier, model=DENIED_SMALL), operation_context())
        assert str(caught.value) == BLOCKED % DENIED_SMALL
    assert inner.calls == [] and inner.routes == []


def test_a_tier_bound_to_a_denied_model_is_refused(catalog, monkeypatch):
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    inner, tool = tool_gateway()
    with pytest.raises(Forbidden) as caught:
        tool.generate(request("code"), operation_context())
    assert str(caught.value) == BLOCKED % DENIED
    assert inner.calls == []


def test_a_name_added_by_the_policy_is_refused(catalog, isolated):
    write_policy(isolated, {"tool_lane_denied_models": {"names": [ALLOWED]}})
    inner, tool = tool_gateway()
    with pytest.raises(Forbidden) as caught:
        tool.generate(request(ALLOWED), operation_context())
    assert str(caught.value) == BLOCKED % ALLOWED
    assert inner.calls == []


def test_an_unreadable_policy_refuses_a_named_model_and_names_the_fix(catalog, isolated):
    write_policy(isolated, "{not json")
    inner, tool = tool_gateway()
    for call in (tool.generate, tool.resolve_route):
        with pytest.raises(Forbidden) as caught:
            call(request(ALLOWED), operation_context())
        assert str(caught.value) == UNREADABLE % ALLOWED
    assert "/runtime reset" in str(caught.value)
    assert inner.calls == [] and inner.routes == []
    # A tier default is not a caller-named model.
    assert tool.generate(request("code"), operation_context()).text == "answered"


PROVIDERS = (
    pytest.param("sonder-inference", "sonder_inference",
                 {"SONDER_INFERENCE_TIER_MODELS": "code=astra-cyber-9b"}, "astra-cyber-9b", id="sonder-inference"),
    pytest.param("openrouter", "openrouter",
                 {"SONDER_OPENROUTER_TIER_MODELS": "code=acme/astra-cyber-27b"}, "acme/astra-cyber-27b", id="openrouter"),
    pytest.param("openai_compatible", "openai_compatible",
                 {"SONDER_OPENAI_MODEL": "Astra-Cyber-27B"}, "Astra-Cyber-27B", id="openai-compatible"),
)


def bind_code(monkeypatch, provider, env):
    monkeypatch.setenv("SONDER_CODE_PROVIDER", provider)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("provider, label, env, served", PROVIDERS)
def test_a_denied_served_model_is_refused_on_each_provider_kind(catalog, monkeypatch, provider, label, env, served):
    bind_code(monkeypatch, provider, env)
    inner, tool = tool_gateway()
    expected = served_refusal("code", label, served)
    for call in (tool.generate, tool.resolve_route):
        with pytest.raises(Forbidden) as caught:
            call(request("code"), operation_context())
        assert str(caught.value) == expected
    assert inner.calls == [] and inner.routes == []
    # Another tier is still on Ollama and passes.
    assert tool.generate(request("fast"), operation_context()).text == "answered"


@pytest.mark.parametrize("provider, label, env, served", PROVIDERS)
def test_an_explicit_denied_model_on_a_bridged_tier_is_refused(catalog, monkeypatch, provider, label, env, served):
    bind_code(monkeypatch, provider, env)
    inner, tool = tool_gateway()
    with pytest.raises(Forbidden) as caught:
        tool.generate(request("code", model="astra-cyber-9b"), operation_context())
    assert "astra-cyber-9b" in str(caught.value)
    assert inner.calls == []


@pytest.mark.parametrize("provider, label, env", [
    pytest.param("openai_compatible", "openai_compatible", {}, id="openai-no-model"),
])
def test_an_unknown_served_identity_is_refused(catalog, monkeypatch, provider, label, env):
    bind_code(monkeypatch, provider, env)
    inner, tool = tool_gateway()
    for call in (tool.generate, tool.resolve_route):
        with pytest.raises(Forbidden) as caught:
            call(request("code"), operation_context())
        assert str(caught.value) == (
            UNKNOWN_OPENAI % "code" if provider == "openai_compatible" else UNKNOWN % ("code", label))
    assert inner.calls == [] and inner.routes == []


def test_a_uniform_backend_judges_the_default_model_and_any_name(catalog, monkeypatch):
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", ALLOWED)
    inner, tool = tool_gateway()
    assert tool.generate(request("fast"), operation_context()).text == "answered"
    with pytest.raises(Forbidden, match="blocked from tool-using lanes"):
        tool.generate(request("astra-cyber-70b"), operation_context())
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", "astra-cyber-9b")
    with pytest.raises(Forbidden) as caught:
        tool.generate(request("sonder"), operation_context())
    assert "astra-cyber-9b" in str(caught.value)
    monkeypatch.delenv("SONDER_INFERENCE_MODEL")
    # No model named: the reply names it ("inner", ordinary), and that is what is judged.
    assert tool.generate(request("fast"), operation_context()).text == "answered"
    assert [call.tier for call in inner.calls] == ["fast", "fast"]


@pytest.mark.parametrize("provider", ["sonder-inference", "openrouter"])
def test_single_provider_chat_judges_the_model_served_for_every_name(monkeypatch, provider):
    """Use the real one-provider factory; only the HTTP transport is faked."""
    from sonder_runtime.adapters.inference.openrouter_gateway import OpenRouterGateway
    from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceGateway
    from sonder_runtime.adapters.model_gateway_factory import build_model_gateway
    from tests.test_sonder_inference_gateway import FakeInference, _chat

    monkeypatch.setenv("SONDER_MODEL_BACKEND", provider)
    sent = []
    if provider == "sonder-inference":
        fake = FakeInference(chat=lambda *args: _chat(model=ALLOWED))
        sent = fake.posts
        monkeypatch.setenv("SONDER_INFERENCE_MODEL", DENIED)
        factory = lambda: SonderInferenceGateway(transport=fake.post, get_transport=fake.get)
    else:
        from tests.test_openrouter_gateway import _chat as router_chat

        def post(url, payload, headers, timeout):
            sent.append(payload)
            return router_chat(model="anthropic/claude-sonnet-4")

        monkeypatch.setenv("SONDER_OPENROUTER_MODEL", "team/" + DENIED)
        monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
        from tests.test_openrouter_gateway import FAKE_KEY

        monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
        factory = lambda: OpenRouterGateway(transport=post)
    key = "sonder_inference" if provider == "sonder-inference" else "openrouter"
    raw = build_model_gateway(backend=provider, provider_factories={key: factory})
    chat = ServedTierGateway(raw, bindings=provider_bindings_from_env())
    for name in ("other-model", "local"):
        with pytest.raises(Forbidden, match="blocked by runtime policy"):
            chat.generate(request(name), operation_context(cloud_allowed=True))
    assert sent == []


@pytest.mark.parametrize("provider", ["sonder-inference", "openrouter"])
def test_single_provider_tool_lane_rejects_a_denied_reported_model(monkeypatch, catalog, provider):
    from sonder_runtime.adapters.inference.openrouter_gateway import OpenRouterGateway
    from sonder_runtime.adapters.inference.sonder_inference_gateway import SonderInferenceGateway
    from sonder_runtime.adapters.model_gateway_factory import build_model_gateway
    from tests.test_sonder_inference_gateway import FakeInference, _chat, _health

    monkeypatch.setenv("SONDER_MODEL_BACKEND", provider)
    monkeypatch.setenv(
        "SONDER_INFERENCE_MODEL" if provider == "sonder-inference" else "SONDER_OPENROUTER_MODEL",
        "anthropic/claude-sonnet-4",
    )
    if provider == "sonder-inference":
        fake = FakeInference(health=_health(models=("anthropic/claude-sonnet-4", DENIED)),
                             chat=lambda *args: _chat(model=DENIED))
        factory = lambda: SonderInferenceGateway(transport=fake.post, get_transport=fake.get)
    else:
        from tests.test_openrouter_gateway import FAKE_KEY, _chat as router_chat

        monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
        monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_KEY)
        factory = lambda: OpenRouterGateway(
            transport=lambda *args: router_chat(model=DENIED),
        )
    key = "sonder_inference" if provider == "sonder-inference" else "openrouter"
    inner = build_model_gateway(backend=provider, provider_factories={key: factory})
    tool = ToolLaneGateway(inner, resolve_target=adapter().resolve_target,
                           bindings=provider_bindings_from_env())
    with pytest.raises(Forbidden, match="blocked by runtime policy"):
        tool.generate(request(ALLOWED), operation_context(cloud_allowed=True))


@pytest.mark.parametrize("provider", ["sonder-inference", "openrouter"])
def test_single_provider_chat_rejects_a_denied_reported_model(monkeypatch, provider):
    monkeypatch.setenv("SONDER_MODEL_BACKEND", provider)
    monkeypatch.setenv(
        "SONDER_INFERENCE_MODEL" if provider == "sonder-inference" else "SONDER_OPENROUTER_MODEL",
        "anthropic/claude-sonnet-4",
    )
    from sonder_runtime.application.ports.model_gateway import ModelResponse

    inner = RecordingGateway()
    inner.generate = lambda req, ctx: ModelResponse(text="unsafe", model=DENIED, tier=req.tier, tokens_out=1)
    chat = ServedTierGateway(inner, bindings=provider_bindings_from_env())
    for name in ("other-model", "local"):
        with pytest.raises(Forbidden, match="blocked by runtime policy"):
            chat.generate(request(name), operation_context())


def test_chat_gateway_judges_an_explicit_model_option_before_sending(monkeypatch):
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", ALLOWED)
    inner = RecordingGateway()
    chat = ServedTierGateway(inner, bindings=provider_bindings_from_env())
    with pytest.raises(Forbidden, match="blocked by runtime policy"):
        chat.generate(request("code", model=DENIED), operation_context())
    assert inner.calls == []
    assert chat.generate(request("code", model=ALLOWED), operation_context()).text == "answered"
    assert [call.options["model"] for call in inner.calls] == [ALLOWED]


def test_tool_lane_rejects_a_reply_that_only_echoes_the_tier(monkeypatch, catalog):
    bind_code(monkeypatch, "sonder-inference", {"SONDER_INFERENCE_TIER_MODELS": "code=" + ALLOWED})
    inner = RecordingGateway()
    from sonder_runtime.application.ports.model_gateway import ModelResponse

    inner.generate = lambda req, ctx: ModelResponse(text="unsafe", model="code", tier=req.tier, tokens_out=1)
    tool = ToolLaneGateway(inner, resolve_target=adapter().resolve_target,
                           bindings=provider_bindings_from_env())
    with pytest.raises(Forbidden, match="cannot be determined"):
        tool.generate(request("code"), operation_context())


def test_a_provider_configuration_that_cannot_be_read_is_refused(catalog, monkeypatch):
    bind_code(monkeypatch, "sonder-inference", {"SONDER_INFERENCE_TIER_MODELS": "not-a-pair"})
    inner, tool = tool_gateway()
    with pytest.raises(Forbidden) as caught:
        tool.generate(request("code"), operation_context())
    assert str(caught.value) == UNKNOWN % ("code", "a non-Ollama provider")
    assert inner.calls == []


# ---- pass-through ------------------------------------------------------------

def test_an_ordinary_model_reaches_the_inner_gateway_unchanged(catalog):
    inner, tool = tool_gateway()
    context = operation_context()
    ordinary = request(ALLOWED, num_predict=64)
    response = tool.generate(ordinary, context)
    assert response.text == "answered"
    assert len(inner.calls) == 1 and inner.calls[0] is ordinary
    assert ordinary.options == {"num_predict": 64}
    assert tool.resolve_route(ordinary, context) == "route-sentinel"
    assert inner.routes == [ordinary]


@pytest.mark.parametrize("tier", ["fast", "code", "general", "sonder", ""])
def test_the_ordinary_tiers_pass(catalog, tier):
    inner, tool = tool_gateway()
    assert tool.generate(request(tier), operation_context()).text == "answered"
    assert [call.tier for call in inner.calls] == [tier]


def test_a_served_model_that_is_allowed_passes(catalog, monkeypatch):
    bind_code(monkeypatch, "sonder-inference", {"SONDER_INFERENCE_TIER_MODELS": "code=" + ALLOWED})
    inner, tool = tool_gateway()
    assert tool.generate(request("code"), operation_context()).text == "answered"
    assert len(inner.calls) == 1


def test_resolve_route_returns_none_when_the_inner_gateway_cannot_resolve(catalog):
    class NoRoute:
        def generate(self, request, context):  # pragma: no cover - not called
            raise AssertionError

    _inner, tool = tool_gateway(NoRoute())
    assert tool.resolve_route(request(ALLOWED), operation_context()) is None


def test_embed_passes_through_without_a_check(catalog):
    inner, tool = tool_gateway()
    context = operation_context()
    assert tool.embed([DENIED, "text"], context) == ["embedding-sentinel"]
    assert inner.embeds == [[DENIED, "text"]]
    assert inner.calls == [] and inner.routes == []


def test_nothing_else_is_reachable_through_the_wrapper(catalog):
    inner, tool = tool_gateway()
    public = sorted(name for name in dir(tool) if not name.startswith("_"))
    assert public == ["embed", "generate", "resolve_route"]
    assert not hasattr(tool, "other_entry_point")
    with pytest.raises(AttributeError):
        tool.other_entry_point()
    assert inner.calls == []


# ---- text callers keep chatting with a named denied model --------------------

def text_gateway(used):
    from sonder_runtime.adapters.inference.ollama_gateway import OllamaGateway

    def factory(model, system, temperature, num_predict, num_ctx, **kwargs):
        used.append(model)
        return lambda prompt, history=None: f"hello from {model}"

    return OllamaGateway(target_resolver=adapter().resolve_target, generate_factory=factory)


@pytest.fixture
def local_endpoint(monkeypatch):
    monkeypatch.setattr(
        "sonder_runtime.adapters.inference.ollama_gateway._enforce_local_endpoint", lambda *a, **k: None)


@pytest.mark.parametrize("name", [DENIED, "Astra-Cyber-9B", HF_QWEN])
def test_a_text_caller_answers_a_named_denied_model(catalog, local_endpoint, name):
    used = []
    gateway = text_gateway(used)
    answer = gateway.generate(ModelRequest("hi", tier=name), operation_context()).text
    assert answer.casefold() == ("hello from " + name).casefold()
    assert [model.casefold() for model in used] == [name.casefold()]
    assert adapter().resolve_target(name, None).model.casefold() == name.casefold()


def test_the_chat_service_answers_a_named_denied_model_and_an_ordinary_one(catalog, local_endpoint):
    from sonder_runtime.application.chat.handle_chat import ChatCommand, ChatService

    used = []
    chat = ChatService(text_gateway(used), None)
    assert "hello from " + DENIED in chat.complete(ChatCommand(content="hi", tier=DENIED), operation_context()).response_text
    assert "hello from " + ALLOWED in chat.complete(ChatCommand(content="hi", tier=ALLOWED), operation_context()).response_text
    assert used == [DENIED, ALLOWED]


def test_a_text_caller_still_answers_while_the_policy_is_unreadable(catalog, local_endpoint, isolated):
    write_policy(isolated, "{not json")
    used = []
    gateway = text_gateway(used)
    assert gateway.generate(ModelRequest("hi", tier=ALLOWED), operation_context()).text == "hello from " + ALLOWED
    assert gateway.generate(ModelRequest("hi", tier=DENIED), operation_context()).text == "hello from " + DENIED
    assert used == [ALLOWED, DENIED]
    # The tool wrapper over the same resolver refuses the same ordinary name.
    _inner, tool = tool_gateway()
    with pytest.raises(Forbidden) as caught:
        tool.generate(request(ALLOWED), operation_context())
    assert str(caught.value) == UNREADABLE % ALLOWED


def test_the_bootstrap_adapter_resolves_for_chat_and_the_server_default_still_refuses(catalog):
    target = adapter().resolve_target(DENIED, None)
    assert (target.model, target.tier_label) == (DENIED, "model:" + DENIED)
    assert server._serve_target(DENIED, None)[3] == guard.DENIED_LABEL


# ---- build_fix candidates ----------------------------------------------------

def test_build_fix_turns_the_refusal_into_its_residency_refused_result(catalog):
    from sonder_runtime.adapters.build.candidates import ModelCandidateGenerator
    from sonder_runtime.application.build.fix_ports import ResidencyRefused
    from sonder_runtime.bootstrap.build_tools import _LazyModelGateway
    from sonder_runtime.domain.build.repair import RepairEvidence

    inner, tool = tool_gateway()
    evidence = RepairEvidence(target="core", config="Debug", focus_file="a.cpp",
                              source_window="int f() { return 0 }", diagnostics=())
    for name in (DENIED, "Astra-Cyber-9B:latest", HF_ORNITH):
        generator = ModelCandidateGenerator(_LazyModelGateway(lambda: tool), route=name)
        with pytest.raises(ResidencyRefused) as caught:
            generator.propose(evidence, operation_context())
        assert str(caught.value) == BLOCKED % name
        assert not str(caught.value).startswith("ERROR")
    assert inner.calls == [] and inner.routes == []


def test_build_fix_classifies_an_ordinary_route_through_the_inner_gateway(catalog):
    from sonder_runtime.adapters.build.candidates import ModelCandidateGenerator
    from sonder_runtime.bootstrap.build_tools import _LazyModelGateway

    inner = RecordingGateway()
    inner.resolve_route = lambda req, ctx: inner.routes.append(req) or SimpleNamespace(cloud=False)
    _inner, tool = tool_gateway(inner)
    generator = ModelCandidateGenerator(_LazyModelGateway(lambda: tool), route=ALLOWED)
    assert generator.residency(ALLOWED, operation_context()) == "local"
    assert [req.tier for req in inner.routes] == [ALLOWED]


def test_build_fix_refuses_a_denied_served_model(catalog, monkeypatch):
    from sonder_runtime.adapters.build.candidates import ModelCandidateGenerator
    from sonder_runtime.application.build.fix_ports import ResidencyRefused
    from sonder_runtime.bootstrap.build_tools import _LazyModelGateway

    bind_code(monkeypatch, "sonder-inference", {"SONDER_INFERENCE_TIER_MODELS": "code=astra-cyber-9b"})
    inner, tool = tool_gateway()
    generator = ModelCandidateGenerator(_LazyModelGateway(lambda: tool), route="code")
    with pytest.raises(ResidencyRefused) as caught:
        generator.residency("code", operation_context())
    assert str(caught.value) == served_refusal("code", "sonder_inference", "astra-cyber-9b")
    assert inner.routes == []


# ---- the composition root ----------------------------------------------------

def test_the_composition_root_hands_the_wrapped_gateway_to_the_tool_consumers_only(monkeypatch):
    """Build the application with the consumers spied on; check which gateway each one received."""
    import sonder_runtime.adapters.conversational_subagents as subagents
    import sonder_runtime.application.agents.interactive_lanes as lanes
    import sonder_runtime.bootstrap.app as app_module

    seen = {}

    def spy_class(module, name, key):
        original = getattr(module, name)

        class Spy(original):
            def __init__(self, *args, **kwargs):
                seen[key] = (args, kwargs)
                super().__init__(*args, **kwargs)

        monkeypatch.setattr(module, name, Spy)

    spy_class(lanes, "AgentLaneService", "lanes")
    spy_class(app_module, "ChatService", "chat")
    spy_class(app_module, "VisionService", "vision")

    original_runner = subagents.conversational_runner_factory

    def runner_spy(gateway, *args, **kwargs):
        seen["subagents"] = gateway
        return original_runner(gateway, *args, **kwargs)

    monkeypatch.setattr(subagents, "conversational_runner_factory", runner_spy)
    original_tools = app_module._compose_build_tools

    def build_tools_spy(*args, **kwargs):
        seen["build_fix"] = kwargs["model_gateway_getter"]()
        return original_tools(*args, **kwargs)

    monkeypatch.setattr(app_module, "_compose_build_tools", build_tools_spy)
    application = app_module.build_application()
    application.agent_lanes()  # both are built lazily
    application.delegation_service()

    raw = application.model_gateway
    assert not isinstance(raw, ToolLaneGateway)
    # Text consumers: the raw gateway (ChatService through the served-tier check).
    assert isinstance(seen["chat"][0][0], ServedTierGateway) and seen["chat"][0][0]._gateway is raw
    assert not isinstance(seen["chat"][0][0], ToolLaneGateway)
    vision_gateway = seen["vision"][0][1]
    assert not isinstance(vision_gateway, ToolLaneGateway)
    # Tool and apply consumers: the wrapper over that same raw gateway.
    wrapped = {
        "build_fix": seen["build_fix"],
        "interactive lanes": seen["lanes"][0][2],
        "conversational subagents": seen["subagents"],
    }
    for consumer, gateway in wrapped.items():
        assert isinstance(gateway, ToolLaneGateway), consumer
        assert gateway._gateway is raw, consumer
    assert len({id(gateway) for gateway in wrapped.values()}) == 1  # one shared wrapper


# ---- an interactive lane's per-step route check ------------------------------

def lane_request(gateway, *, cloud_allowed=True):
    """One step of an interactive lane up to its route check; the rest of the service is absent."""
    from sonder_runtime.application.agents.interactive_lanes import AgentLaneService

    service = SimpleNamespace(gateway=gateway)
    lane = {"tier": "code", "expensive_tier": False}
    return AgentLaneService._request(
        service, lane, [{"author": "user", "content": "list files"}],
        context=operation_context(cloud_allowed=cloud_allowed),
    )


def test_a_lane_step_refused_by_the_deny_list_shows_the_deny_text(catalog, monkeypatch):
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    inner, tool = tool_gateway()
    with pytest.raises(PermissionError) as caught:
        lane_request(tool)
    assert str(caught.value) == BLOCKED % DENIED
    assert inner.calls == [] and inner.routes == []


def test_a_lane_step_on_a_denied_served_model_shows_the_served_text(catalog, monkeypatch):
    bind_code(monkeypatch, "sonder-inference", {"SONDER_INFERENCE_TIER_MODELS": "code=" + DENIED_SMALL})
    inner, tool = tool_gateway()
    with pytest.raises(PermissionError) as caught:
        lane_request(tool)
    assert str(caught.value) == served_refusal("code", "sonder_inference", DENIED_SMALL)
    assert inner.calls == [] and inner.routes == []


def test_a_lane_step_whose_route_fails_otherwise_keeps_the_spawn_budget_text(catalog):
    """The control: only the deny list's refusal changes the message."""
    class Broken:
        def resolve_route(self, request, context):
            raise RuntimeError("catalog offline")

    with pytest.raises(PermissionError, match="unverified lane route could exceed its spawn budget"):
        lane_request(Broken())

