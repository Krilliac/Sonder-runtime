"""The legacy HTTP chat path reaches non-Ollama providers through the gateway.

Ollama is unreachable in every test here: ``_post_model`` (the only road to
Ollama's /api/chat) fails the test if it is called on a bridged rung, and the
Ollama-only probes (context size, cache revision) do the same.
"""
from contextlib import contextmanager
import http.client
import json
import logging
import threading
import urllib.error
from types import SimpleNamespace

import pytest

import server
import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission
from sonder_runtime.adapters.provider_bindings import ProviderBindings
from sonder_runtime.adapters.provider_dispatch.gateway import ProviderDispatchGateway
from sonder_runtime.application.chat import provider_bridge
from sonder_runtime.application.context import (
    bind_operation_context,
    local_owner_context,
)
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.application.routing import tier_escalation


class _Connection:
    def close(self):
        return None


class RecordingGateway:
    """Wrap the real dispatch gateway and record what the bridge sends."""

    def __init__(self, inner):
        self.inner = inner
        self.calls = []

    def generate(self, request, context):
        self.calls.append((request, context, provider_bridge.active_rung()))
        return self.inner.generate(request, context)

    def embed(self, texts, context):
        return self.inner.embed(texts, context)


class OllamaMustNotBeUsed:
    def generate(self, request, context):
        pytest.fail("the Ollama gateway was used for a non-Ollama tier")

    def embed(self, texts, context):
        pytest.fail("unexpected embedding")


def _openai_transport(reply=None, error=None):
    sent = []

    def transport(url, payload, headers, timeout):
        sent.append({"url": url, "payload": payload})
        if error is not None:
            raise error
        return reply or {
            "model": "fake",
            "choices": [{"message": {"role": "assistant", "content": "provider answer"}}],
            "usage": {"prompt_tokens": 11, "completion_tokens": 3},
        }

    transport.sent = sent
    return transport


def _bindings(provider="openai_compatible"):
    return ProviderBindings(
        default_generation_provider=provider,
        tier_providers={tier: provider for tier in
                        ("fast", "general", "code", "reasoning", "vision")},
        embedding_provider="ollama",
    )


def _graph(transport, *, provider="openai_compatible", ollama=None):
    openai = OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url="http://127.0.0.1:18999", model="fake"),
        transport=transport,
        request_admission=HostModelRequestAdmission(None),
    )
    bindings = _bindings(provider)
    dispatch = ProviderDispatchGateway(
        providers={"openai_compatible": openai, "ollama": ollama or OllamaMustNotBeUsed()},
        tier_providers=dict(bindings.tier_providers),
        default_generation_provider=bindings.default_generation_provider,
        embedding_provider=bindings.embedding_provider,
    )
    return SimpleNamespace(provider_bindings=bindings, model_gateway=RecordingGateway(dispatch))


@pytest.fixture
def no_ollama(monkeypatch):
    posts = []

    def refuse_post(path, payload, **kwargs):
        posts.append(path)
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(server, "_post_model", refuse_post)
    monkeypatch.setattr(server, "_auto_model_context",
                        lambda model: pytest.fail("Ollama context probe on a bridged rung"))
    monkeypatch.setattr(server, "_cache_model_revision",
                        lambda model: pytest.fail("Ollama revision probe on a bridged rung"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "control_command", lambda *a, **k: None)
    monkeypatch.setattr(server, "_web_denial_guard", lambda *a, **k: None)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(server, "_build_system", lambda *a, **k: "sys")
    monkeypatch.setattr(server, "_should_learn", lambda *_a: False)
    monkeypatch.setattr(server, "_resolve_project", lambda *_a: None)
    monkeypatch.setattr(server, "_capture_turn", lambda *a, **k: None)
    monkeypatch.setattr(server, "_capture_durable_session_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        server, "_apply_code_gate",
        lambda response, interaction_id=None, regenerate=None: (response, None, False),
    )
    monkeypatch.setattr(
        server, "_serve_target",
        lambda tier, strict: ("qwen2.5-coder:7b", False, False,
                              "general" if (tier or "sonder") == "sonder" else tier),
    )
    return posts


def _install(monkeypatch, graph):
    monkeypatch.setattr(server, "_APP_GRAPH", graph)


def test_non_ollama_rung_answers_through_the_gateway(monkeypatch, no_ollama):
    transport = _openai_transport()
    graph = _graph(transport)
    _install(monkeypatch, graph)
    reply = server._answer_with_history_impl("hello", [], tier="general",
                                             raise_model_errors=True)
    assert "provider answer" in reply
    assert no_ollama == []
    assert len(transport.sent) == 1
    sent = transport.sent[0]["payload"]
    assert sent["model"] == "fake"  # the Ollama model name is never forwarded
    assert sent["messages"][-1] == {"role": "user", "content": "hello"}
    request, _context, rung_during_call = graph.model_gateway.calls[0]
    assert request.tier == "general"
    assert rung_during_call is None


def test_all_ollama_bindings_leave_the_legacy_path_untouched(monkeypatch):
    posts = []

    def fake_post(path, payload, **kwargs):
        posts.append((path, payload["model"]))
        return {"message": {"role": "assistant", "content": "ollama answer"}}, 1

    monkeypatch.setattr(server, "_post_model", fake_post)
    monkeypatch.setattr(server, "_known_thinking_model", lambda model: False)
    graph = SimpleNamespace(provider_bindings=ProviderBindings.uniform("ollama"),
                            model_gateway=OllamaMustNotBeUsed())
    _install(monkeypatch, graph)
    with provider_bridge.bind_rung(server._bridge_provider_for_tier("general"), "general"):
        out, content = server._chat_request(
            {"model": "qwen", "messages": [{"role": "user", "content": "x"}],
             "options": {}, "think": False},
            model="qwen",
        )
    assert content == "ollama answer"
    assert posts == [("/api/chat", "qwen")]


def test_provider_outage_is_a_terminal_503_without_escalation(monkeypatch, no_ollama):
    transport = _openai_transport(error=urllib.error.URLError("Connection refused"))
    graph = _graph(transport)
    _install(monkeypatch, graph)
    second = tier_escalation.Rung(tier="reasoning", model="other:14b", cloud=False, augment=False)
    monkeypatch.setattr(server, "_default_route_plan",
                        lambda prompt, start, **k: tier_escalation.Plan("chat", 1.0, (start, second)))
    with pytest.raises(server.ModelCallError) as caught:
        server._answer_with_history_impl("hello", [], raise_model_errors=True)
    assert caught.value.status == 503
    assert caught.value.kind == provider_bridge.PROVIDER_UNAVAILABLE_KIND
    assert "openai_compatible" in caught.value.detail
    assert len(graph.model_gateway.calls) == 1  # never stepped to the next rung
    assert no_ollama == []


@pytest.mark.parametrize("payload_extra,kwargs", [
    ({"think": True}, {}),
    ({"format": {"type": "object"}}, {}),
    ({"tools": [{"type": "function"}]}, {}),
])
def test_ollama_only_features_are_a_400(monkeypatch, no_ollama, payload_extra, kwargs):
    _install(monkeypatch, _graph(_openai_transport()))
    payload = {"model": "qwen", "messages": [{"role": "user", "content": "x"}],
               "options": {}, **payload_extra}
    with provider_bridge.bind_rung("openai_compatible", "general"):
        with pytest.raises(server.ModelCallError) as caught:
            server._chat_request(payload, model="qwen", **kwargs)
    assert caught.value.status == 400
    assert caught.value.kind == provider_bridge.UNSUPPORTED_FEATURE_KIND


def test_reasoning_continuation_is_a_400(monkeypatch, no_ollama):
    _install(monkeypatch, _graph(_openai_transport()))
    with provider_bridge.bind_rung("openai_compatible", "general"):
        with pytest.raises(server.ModelCallError) as caught:
            server._chat_request(
                {"model": "q", "messages": [{"role": "user", "content": "x"}],
                 "options": {"num_predict": 64}},
                model="q", reasoning_continuation=True,
            )
    assert caught.value.status == 400


def test_structured_output_on_a_bridged_tier_is_refused(monkeypatch, no_ollama):
    _install(monkeypatch, _graph(_openai_transport()))
    with pytest.raises(server.ModelCallError) as caught:
        server.structured_answer_with_history("x", [], {"type": "object"}, tier="general")
    assert caught.value.status == 400


def test_gateway_fallback_to_ollama_does_not_re_enter_the_bridge(monkeypatch):
    """A1: the Ollama gateway re-enters _chat_request with the rung suspended."""
    posts = []

    def fake_post(path, payload, **kwargs):
        posts.append(path)
        return {"message": {"role": "assistant", "content": "from ollama"}}, 1

    monkeypatch.setattr(server, "_post_model", fake_post)
    monkeypatch.setattr(server, "_known_thinking_model", lambda model: False)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 4096)

    class FallsBackToOllama:
        def generate(self, request, context):
            # What OllamaGateway does: build the legacy generator and call it.
            text = server._make_generate("qwen", "", 0.2, 64, 4096, think=False)(request.prompt)
            return ModelResponse(text=text, model="qwen", tier=request.tier)

    graph = SimpleNamespace(provider_bindings=_bindings(), model_gateway=FallsBackToOllama())
    _install(monkeypatch, graph)
    with provider_bridge.bind_rung("openai_compatible", "general"):
        out, content = server._chat_request(
            {"model": "qwen", "messages": [{"role": "user", "content": "x"}], "options": {}},
            model="qwen",
        )
    assert content == "from ollama"
    assert posts == ["/api/chat"]


def test_gateway_offload_inside_a_rung_takes_its_own_route(monkeypatch):
    seen = {}

    class Chat:
        def complete(self, command, context):
            seen["rung"] = provider_bridge.active_rung()
            seen["correlation"] = context.correlation_id
            return SimpleNamespace(response_text="title")

    monkeypatch.setattr(server, "_APP_GRAPH", SimpleNamespace(chat=Chat()))
    turn = local_owner_context(correlation_id="req-turn-7", source="http")
    with bind_operation_context(turn), provider_bridge.bind_rung("openai_compatible", "general"):
        assert server._gateway_generate_text("summarise", tier="fast") == "title"
    assert seen == {"rung": None, "correlation": "req-turn-7"}


def test_missing_embeddings_on_a_bridged_turn_degrade_loudly(monkeypatch, caplog):
    monkeypatch.setattr(server.embeddings, "embed", lambda text: None)
    monkeypatch.setattr(server, "_preference_facts", lambda *a, **k: [])
    monkeypatch.setattr(server, "_capture_preferences", lambda *a, **k: None)
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: (lambda *x, **y: "ok"))
    monkeypatch.setattr(server.orchestrator, "run_with_learning",
                        lambda *a, **k: ("answer", None))
    with provider_bridge.degradation_scope() as notes, \
            provider_bridge.bind_rung("openai_compatible", "general"), \
            caplog.at_level(logging.WARNING, logger="sonder.server"):
        response, _iid, _trace = server._answer(
            _Connection(), "hello", "qwen", "sys", 0.2, 64, None, None, None, None,
        )
    assert response == "answer"
    assert notes == ["memory_recall_embeddings"]
    assert any("memory_recall_embeddings" in r.getMessage() for r in caplog.records)


def test_prewarm_skips_tiers_served_by_another_provider(monkeypatch):
    monkeypatch.setattr(server.sonder_speculation, "speculation_enabled", lambda: True)
    monkeypatch.setattr(server, "_serve_target",
                        lambda tier, strict: ("qwen", False, False, "general"))
    monkeypatch.setattr(server, "_post", lambda *a, **k: pytest.fail("prewarm reached Ollama"))
    _install(monkeypatch, SimpleNamespace(provider_bindings=_bindings(),
                                          model_gateway=OllamaMustNotBeUsed()))
    assert server.prewarm_model("general") is False


# --- over HTTP -----------------------------------------------------------------


@contextmanager
def _http(monkeypatch, *, stub_web=True):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    if stub_web:
        monkeypatch.setattr(ts.server, "chat_web_response", lambda *a, **k: None)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post_chat(port, body):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    conn.request("POST", "/v1/chat/completions", body=json.dumps(body),
                 headers={"Content-Type": "application/json"})
    response = conn.getresponse()
    payload = response.read()
    headers = {k.lower(): v for k, v in response.getheaders()}
    conn.close()
    return response.status, headers, payload


@pytest.mark.parametrize("model", ["sonder", "general"])
def test_http_chat_reaches_the_provider_with_ollama_down(monkeypatch, no_ollama, model):
    transport = _openai_transport()
    graph = _graph(transport)
    _install(monkeypatch, graph)
    with _http(monkeypatch) as port:
        status, headers, payload = _post_chat(port, {
            "model": model, "messages": [{"role": "user", "content": "hello"}],
        })
    assert status == 200, payload
    body = json.loads(payload)
    assert body["choices"][0]["message"]["content"] == "provider answer"
    assert body["usage"]["prompt_tokens"] == 11
    assert body["usage"]["completion_tokens"] == 3
    assert body["sonder_receipt"]["model"] == "fake"
    assert no_ollama == []
    _request, context, _rung = graph.model_gateway.calls[0]
    assert context.correlation_id == headers["x-sonder-correlation-id"]
    assert context.source == "http"


def test_http_chat_provider_outage_returns_503(monkeypatch, no_ollama):
    graph = _graph(_openai_transport(error=urllib.error.URLError("Connection refused")))
    _install(monkeypatch, graph)
    with _http(monkeypatch) as port:
        status, headers, payload = _post_chat(port, {
            "model": "general", "messages": [{"role": "user", "content": "hello"}],
        })
    assert status == 503, payload
    assert "openai_compatible" in json.loads(payload)["error"]["message"]
    assert headers.get("retry-after")


def test_strict_sonder_alias_stays_on_ollama_like_a2a(monkeypatch):
    """A resolved ``sonder`` label is the local alias; ProviderDispatchGateway
    serves it only from Ollama, so the HTTP path must not bridge it."""
    _install(monkeypatch, SimpleNamespace(provider_bindings=_bindings(),
                                          model_gateway=OllamaMustNotBeUsed()))
    assert server._bridge_provider_for_tier("sonder") is None
    assert server._bridge_provider_for_tier("general") == "openai_compatible"


def test_bridged_step_ignores_the_drain_token_but_honours_cancel_check(monkeypatch):
    from sonder_runtime.platform.process import CancellationToken

    drained = CancellationToken()
    drained.cancel()
    turn = local_owner_context(correlation_id="req-drain-1", source="http",
                               cancellation=drained)
    with bind_operation_context(turn):
        context = server._bridge_operation_context(30, None)
        assert context.correlation_id == "req-drain-1"
        assert context.cancellation.cancelled is False
        assert server._bridge_operation_context(30, lambda: True).cancellation.cancelled


def test_drain_during_a_bridged_call_lets_the_turn_finish(monkeypatch, no_ollama):
    """A drain that starts after admission must not discard the provider's answer."""
    from sonder_runtime.adapters.web import lifecycle as sonder_lifecycle

    lifecycle = sonder_lifecycle.RuntimeLifecycle()
    monkeypatch.setattr(sonder_lifecycle, "_instance", lifecycle)
    in_call, release = threading.Event(), threading.Event()
    base = _openai_transport()

    def slow_transport(url, payload, headers, timeout):
        in_call.set()
        assert release.wait(10)
        return base(url, payload, headers, timeout)

    graph = _graph(slow_transport)
    _install(monkeypatch, graph)
    result = {}
    with _http(monkeypatch) as port:
        def post():
            result["response"] = _post_chat(port, {
                "model": "general", "messages": [{"role": "user", "content": "hello"}],
            })

        client = threading.Thread(target=post)
        client.start()
        assert in_call.wait(10)
        drainer = threading.Thread(target=lambda: lifecycle.coordinator.drain(reason="test"))
        drainer.start()
        for _ in range(200):
            if lifecycle.coordinator.cancellation.cancelled:
                break
            threading.Event().wait(0.01)
        assert lifecycle.coordinator.cancellation.cancelled
        release.set()
        client.join(15)
        drainer.join(15)
    status, _headers, payload = result["response"]
    assert status == 200, payload
    assert json.loads(payload)["choices"][0]["message"]["content"] == "provider answer"
    assert len(base.sent) == 1


def test_http_web_research_on_a_bound_tier_fails_closed(monkeypatch, no_ollama):
    """The research agent only runs on Ollama; a bound tier gets a 503 naming it."""
    transport = _openai_transport()
    _install(monkeypatch, _graph(transport))
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_agent_impl",
                        lambda *a, **k: pytest.fail("the Ollama agent ran for a bound tier"))
    with _http(monkeypatch, stub_web=False) as port:
        status, _headers, payload = _post_chat(port, {
            "model": "sonder",
            "messages": [{"role": "user", "content": "search the web for the latest python release"}],
        })
    assert status == 503, payload
    message = json.loads(payload)["error"]["message"]
    assert "openai_compatible" in message and "'code'" in message
    assert transport.sent == [] and no_ollama == []


def test_web_research_keeps_its_ollama_route_outside_http(monkeypatch, no_ollama):
    """REPL and MCP call chat_web_response without gateway_bound (documented)."""
    _install(monkeypatch, _graph(_openai_transport()))
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_agent_impl", lambda task, **k: "researched")
    assert server.chat_web_response("search the web for the latest python release") == "researched"
