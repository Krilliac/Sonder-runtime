"""A blocked model never serves a tier: the bridge's own check, on every lane.

A bridged call is always a tier call (a model asked for by name stays on
Ollama; the bridged request carries no ``model`` option).  So the legacy bridge
refuses a blocked served model whether or not the call site is text-only:
before sending when the tier's provider is configured to serve it, and after
the reply when the reply reports it.  That check does not depend on any lane's
earlier refusal, so a tier bound while a run is going cannot slip a blocked
model's reply into a lane that runs it.

These tests run the real lanes, ``sonder()``, escalation, rung binding, the
bridge and the guard; only the transports are faked: ``server._post_model``
(Ollama) and the packaged gateway a bridged rung is served by.  Nothing leaves
the process.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env
from sonder_runtime.application.ports.model_gateway import ModelResponse
from tests import test_tool_lane_default_route as default_route
from tests.support.tool_lane import (
    ALLOWED,
    DENIED,
    RUNNABLE,
    UNKNOWN_OPENAI,
    clear_environment,
    served_refusal,
)

# The default-route suite's isolation (autouse) and execution-sink fixtures.
isolated = default_route.isolated
executed = default_route.executed
TASK = default_route.TASK

LATE = "deepseek-r1:8b"  # an ordinary Ollama name bound to ``reasoning`` while a run is going
TASKS = [dict(TASK, name="t1"), dict(TASK, name="t2")]
REASONING_REFUSAL = served_refusal("reasoning", "sonder_inference", DENIED)


class Gateway:
    """The packaged gateway a bridged rung is served by: records each request, reports ``served``."""

    def __init__(self, served, text=RUNNABLE):
        self.calls = []
        self.served = served
        self.text = text

    def generate(self, request, context):
        self.calls.append(request)
        return ModelResponse(text=self.text, model=self.served, tier=request.tier, tokens_out=1)


@pytest.fixture
def ollama(monkeypatch):
    """Fake Ollama transport: every local model answers with nothing, so the route steps up."""
    sent = []

    def fake_post(path, payload, **kwargs):
        sent.append((path, copy.deepcopy(payload)))
        return {
            "model": payload["model"],
            "message": {"role": "assistant", "content": ""},
            "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 0,
        }, 1

    monkeypatch.setattr(server, "_post_model", fake_post)
    monkeypatch.setattr(server, "_known_thinking_model", lambda model: False)
    monkeypatch.setattr(server, "_think_option_supported", lambda model: False)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 4096)
    monkeypatch.setattr(server.embeddings, "embed", lambda *a, **k: None)  # no embedder call either
    return sent


def serve_through(monkeypatch, gateway):
    """Hand the bridge ``gateway`` with the provider bindings the environment now names."""
    monkeypatch.setattr(server, "_APP_GRAPH", SimpleNamespace(
        provider_bindings=provider_bindings_from_env(), model_gateway=gateway,
        recall=SimpleNamespace(retrieve=lambda *a, **k: [])))  # memory recall is not under test
    return gateway


def bridge_reasoning(monkeypatch, served=DENIED, configured=DENIED):
    """``reasoning`` on Sonder Inference (``configured`` names its model; ``""`` names none)."""
    monkeypatch.setenv("SONDER_REASONING_PROVIDER", "sonder-inference")
    if configured:
        monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "reasoning=" + configured)
    return serve_through(monkeypatch, Gateway(served))


def bind_reasoning_on_first_call(monkeypatch, ollama):
    """Bind ``reasoning`` to an ordinary Ollama name when the first model call is sent.

    This is ``runtime_policy_update`` / ``/runtime set`` from another thread
    while a run is going: the lane's one-time refusal has already passed.
    """
    real = server._post_model

    def post(path, payload, **kwargs):
        if len(ollama) == 0:
            policy = dict(server._RUNTIME_POLICY)
            policy["local_models"] = dict(policy["local_models"], reasoning=LATE)
            monkeypatch.setattr(server, "_RUNTIME_POLICY", policy)
            monkeypatch.setitem(server.TIERS, "reasoning", LATE)
        return real(path, payload, **kwargs)

    monkeypatch.setattr(server, "_post_model", post)


@pytest.fixture
def replies(monkeypatch):
    """What each ``sonder()`` call of a lane returned (the real function, observed)."""
    seen = []
    real = server.sonder

    def observed(*args, **kwargs):
        reply = real(*args, **kwargs)
        seen.append(reply)
        return reply

    monkeypatch.setattr(server, "sonder", observed)
    return seen


def campaign(tier=""):
    return server.campaign_generate_compile_execute_record(
        total=2, languages="python", tier=tier, max_workers=1, repair_rounds=0, record_failures=False)


@pytest.fixture
def two_tasks(monkeypatch):
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl

    monkeypatch.setattr(serve.training_tasks, "sample", lambda n: [dict(task) for task in TASKS])
    monkeypatch.setattr(repl.training_tasks, "sample", lambda n: [dict(task) for task in TASKS])


def train_http():
    from sonder_runtime.interfaces.http import serve

    return serve._do_train(2)


def practice(capsys):
    import curriculum_run

    curriculum_run.practice_on([dict(task) for task in TASKS])
    return capsys.readouterr().out


LANES = {"campaign": lambda capsys: campaign(), "train": lambda capsys: train_http(), "practice": practice}


# ---- a tier bound mid-run: the rung is refused at the bridge -------------------

@pytest.mark.parametrize("lane", LANES)
def test_a_tier_bound_mid_run_never_serves_its_blocked_model(
        monkeypatch, capsys, ollama, executed, two_tasks, replies, lane):
    gateway = bridge_reasoning(monkeypatch)
    assert guard.selector_refusal("", server.TIERS, server._RUNTIME_POLICY) == ""  # unbound: the lane starts
    bind_reasoning_on_first_call(monkeypatch, ollama)
    out = LANES[lane](capsys)
    assert server.TIERS.get("reasoning") == LATE  # the binding happened during the run
    assert gateway.calls == []  # the rung was refused at the bridge, before sending
    assert executed == [] and "def double" not in out
    # The run's turn on that rung reports the refusal (the lane's summary keeps its own wording).
    assert any(reply.startswith("ERROR: " + REASONING_REFUSAL) for reply in replies), replies


@pytest.mark.parametrize("lane", LANES)
def test_a_tier_bound_mid_run_whose_reply_reports_a_blocked_model_is_refused(
        monkeypatch, capsys, ollama, executed, two_tasks, replies, lane):
    gateway = bridge_reasoning(monkeypatch, configured="")  # nothing named: the reply decides
    bind_reasoning_on_first_call(monkeypatch, ollama)
    out = LANES[lane](capsys)
    assert len(gateway.calls) == 1  # sent and answered; the reply is dropped
    assert executed == [] and "def double" not in out
    assert any(reply.startswith("ERROR: " + REASONING_REFUSAL) for reply in replies), replies


def test_without_a_blocked_model_the_late_rung_answers_and_its_code_runs(
        monkeypatch, capsys, ollama, executed, two_tasks, replies):
    gateway = bridge_reasoning(monkeypatch, served="vendor/ordinary-8b", configured="vendor/ordinary-8b")
    bind_reasoning_on_first_call(monkeypatch, ollama)
    out = campaign()
    assert len(gateway.calls) == 1 and gateway.calls[0].tier == "reasoning"
    assert len(executed) == 1 and "def double" in executed[0]
    assert "blocked" not in out


# ---- chat by tier on a bridged tier serving a blocked model ---------------------

@pytest.fixture
def chat(monkeypatch, ollama):
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setenv("SONDER_MODEL_ESCALATION", "0")
    return ollama


def bridge_code(monkeypatch, served=DENIED, configured=DENIED, provider="sonder-inference", text=RUNNABLE):
    monkeypatch.setenv("SONDER_CODE_PROVIDER", provider)
    if configured and provider == "sonder-inference":
        monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "code=" + configured)
    if configured and provider == "openai_compatible":
        monkeypatch.setenv("SONDER_OPENAI_MODEL", configured)
    return serve_through(monkeypatch, Gateway(served, text=text))


CODE_REFUSAL = served_refusal("code", "sonder_inference", DENIED)


def mcp_chat():
    return server._sonder_impl("hi", tier="code", session="none", project="none")


def http_chat():
    """The HTTP chat turn; a refused model call reaches the client as the error's detail."""
    from sonder_runtime.interfaces.http import serve

    try:
        return str(serve._run_prompt("hi", [], tier="code", session="none", project="none"))
    except server.ModelCallError as error:
        assert getattr(error, "tool_lane_refused", False) is True
        return error.detail


def ensemble():
    return server.ensemble_answer("hi", tiers="code")


def consult():
    return server.consult("hi", tiers="code,fast")


TEXT_LANES = {"mcp-chat": mcp_chat, "http-chat": http_chat, "ensemble": ensemble, "consult": consult}


@pytest.mark.parametrize("lane", TEXT_LANES)
def test_chat_by_tier_is_refused_before_sending_when_the_tier_serves_a_blocked_model(monkeypatch, chat, lane):
    gateway = bridge_code(monkeypatch)
    out = TEXT_LANES[lane]()
    assert CODE_REFUSAL in out
    assert gateway.calls == []
    assert "def double" not in out


@pytest.mark.parametrize("lane", TEXT_LANES)
def test_chat_by_tier_is_refused_when_the_reply_reports_a_blocked_model(monkeypatch, chat, lane):
    gateway = bridge_code(monkeypatch, configured="")
    out = TEXT_LANES[lane]()
    assert CODE_REFUSAL in out
    assert len(gateway.calls) == 1
    assert "def double" not in out


@pytest.mark.parametrize("lane", TEXT_LANES)
def test_chat_by_tier_on_an_ordinary_bridged_model_answers(monkeypatch, chat, lane):
    gateway = bridge_code(monkeypatch, served="vendor/ordinary-8b", configured="vendor/ordinary-8b",
                          text="bridged answer")
    out = TEXT_LANES[lane]()
    assert "bridged answer" in out and "blocked" not in out
    assert len(gateway.calls) == 1


# ---- an ordinary bridged model: request and reply unchanged on text and exec lanes ----

def test_an_ordinary_bridged_tier_sends_and_returns_the_same_on_text_and_exec_lanes(monkeypatch, ollama):
    gateway = bridge_code(monkeypatch, served="vendor/ordinary-8b", configured="vendor/ordinary-8b",
                          text="bridged answer")
    text = server._make_tier_generate("code", ALLOWED, "sys", 0.2, 64, 0, text_only=True)("hi")
    run = server._make_tier_generate("code", ALLOWED, "sys", 0.2, 64, 0)("hi")
    assert text == run == "bridged answer"
    assert len(gateway.calls) == 2 and gateway.calls[0] == gateway.calls[1]
    assert "model" not in (gateway.calls[0].options or {})  # the bridged request names no model
    assert ollama == []


# ---- Sonder Inference with no model named (S1) -----------------------------------

@pytest.fixture
def inference_backend(monkeypatch):
    """The whole process on Sonder Inference with no model named (shipped default ``default``)."""
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder-inference")


def test_inference_with_no_model_named_is_not_refused_up_front(inference_backend):
    for selector in ("", "sonder", "local", "code", "general", "fast"):
        assert guard.selector_refusal(selector, server.TIERS, server._RUNTIME_POLICY) == "", selector
    model, _cloud, _augment, label = server._serve_target("sonder", None)
    assert guard.resolved_refusal("sonder", model, label, server._RUNTIME_POLICY) == ""  # the agent turn
    assert guard.denied(ALLOWED, "code", server._RUNTIME_POLICY) is False  # the code gate


@pytest.mark.parametrize("text_only", [True, False], ids=["text-lane", "exec-lane"])
def test_inference_with_no_model_named_judges_the_model_the_reply_reports(
        monkeypatch, ollama, inference_backend, text_only):
    gateway = serve_through(monkeypatch, Gateway("vendor/ordinary-8b", text="bridged answer"))
    assert server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0, text_only=text_only)("hi") == "bridged answer"
    gateway.served = DENIED
    gen = server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0, text_only=text_only)
    with pytest.raises(server.ModelCallError) as caught:
        gen("hi")
    assert caught.value.detail == served_refusal("code", "sonder_inference", DENIED)
    assert getattr(caught.value, "tool_lane_refused", False) is True
    assert len(gateway.calls) == 2 and ollama == []


def test_inference_with_no_model_named_runs_the_agent_turn(monkeypatch, ollama, inference_backend):
    """The server's own agent turn on ``sonder`` reaches the model (no up-front refusal)."""
    gateway = serve_through(monkeypatch, Gateway("vendor/ordinary-8b", text='{"final": "done"}'))
    out = server._agent_turn("say done", tier="sonder")
    assert "cannot be determined" not in out and "blocked" not in out
    assert len(gateway.calls) >= 1


def test_an_inference_reply_with_no_model_is_still_rejected_by_the_gateway():
    """The Sonder Inference gateway itself rejects a reply that names no model (unchanged)."""
    import inspect

    from sonder_runtime.adapters.inference import sonder_inference_gateway as inference

    source = inspect.getsource(inference.SonderInferenceGateway)
    assert 'served == DEFAULT_MODEL' in source and "did not name the served model" in source


def test_the_openai_compatible_transport_with_no_model_is_still_refused_up_front(monkeypatch):
    monkeypatch.setenv("SONDER_CODE_PROVIDER", "openai_compatible")
    assert guard.selector_refusal("code", server.TIERS, server._RUNTIME_POLICY) == UNKNOWN_OPENAI % "code"
    assert "SONDER_OPENAI_MODEL" in UNKNOWN_OPENAI


# ---- the packaged text path: ChatService's handle --------------------------------

def chat_service_gateway(monkeypatch, inner):
    from sonder_runtime.adapters.tool_lane_gateway import ServedTierGateway

    return ServedTierGateway(inner, bindings=provider_bindings_from_env())


def packaged(tier, **options):
    from sonder_runtime.application.ports.model_gateway import ModelRequest
    from tests.support.tool_lane import operation_context

    return ModelRequest("hi", tier=tier, options=options or None), operation_context()


def test_chat_service_refuses_a_tier_whose_provider_serves_a_blocked_model(monkeypatch):
    from sonder_runtime.domain.common.errors import Forbidden

    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_CODE_PROVIDER", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "code=" + DENIED)
    inner = Gateway(DENIED, text="text")
    handle = chat_service_gateway(monkeypatch, inner)
    with pytest.raises(Forbidden) as caught:
        handle.generate(*packaged("code"))
    assert str(caught.value) == CODE_REFUSAL and inner.calls == []
    monkeypatch.delenv("SONDER_INFERENCE_TIER_MODELS")  # nothing named: the reply decides
    with pytest.raises(Forbidden) as caught:
        handle.generate(*packaged("code"))
    assert str(caught.value) == CODE_REFUSAL and len(inner.calls) == 1
    inner.served = "vendor/ordinary-8b"
    assert handle.generate(*packaged("code")).text == "text"
    assert handle.generate(*packaged("fast")).text == "text"  # an Ollama tier is not judged here
    assert handle.generate(*packaged(DENIED)).text == "text"  # by name: a text answer


def test_chat_service_handle_keeps_the_wrapped_gateways_other_entry_points():
    from sonder_runtime.adapters.tool_lane_gateway import ServedTierGateway

    inner = SimpleNamespace(generate_strict_alias=lambda request, context: "alias", provider_status=lambda: {})
    handle = ServedTierGateway(inner, bindings=provider_bindings_from_env({}))
    assert handle.generate_strict_alias(None, None) == "alias" and handle.provider_status() == {}
    assert getattr(handle, "absent_entry_point", None) is None
