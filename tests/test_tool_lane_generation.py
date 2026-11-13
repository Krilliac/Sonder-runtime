"""The bottom check: generation refuses a tool-lane-denied model unless declared text-only.

Every model call reaches ``server._chat_request``; it refuses a denied model
(by name, by the payload's model, and for a bridged rung by the model its
provider serves) unless the caller passed ``text_only=True``.  Here the
transport is ``_post_model`` (Ollama) or a recording packaged gateway (a
bridged rung), both faked: nothing leaves the process.  Each refusal is
asserted to have sent nothing, each text-only call to have sent exactly once.
"""
from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.adapters.provider_bindings import provider_bindings_from_env
from sonder_runtime.application.ports.model_gateway import ModelResponse
from tests.support.tool_lane import (
    ALLOWED,
    BLOCKED,
    DENIED,
    HF_ORNITH,
    HF_QWEN,
    SERVED,
    UNKNOWN,
    UNKNOWN_OPENAI,
    clear_environment,
    write_policy,
)

SPELLINGS = (DENIED, "Astra-Cyber-9B", "astra-cyber-27b:latest", HF_QWEN, HF_ORNITH)


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    policy_path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_path))
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None, raising=False)
    before = dict(server.TIERS)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", server._RUNTIME_POLICY)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    server._refresh_runtime_policy(create=False)
    for tier in ("fast", "code", "general"):
        server.TIERS[tier] = ALLOWED
    yield policy_path
    server.TIERS.clear()
    server.TIERS.update(before)


@pytest.fixture
def posts(monkeypatch):
    """Fake Ollama transport: record each send and answer with the model's name."""
    sent = []

    def fake_post(path, payload, **kwargs):
        sent.append((path, copy.deepcopy(payload), dict(kwargs)))
        return {
            "model": payload["model"],
            "message": {"role": "assistant", "content": "answer from " + payload["model"]},
            "done_reason": "stop", "prompt_eval_count": 3, "eval_count": 2,
        }, 1

    monkeypatch.setattr(server, "_post_model", fake_post)
    monkeypatch.setattr(server, "_known_thinking_model", lambda model: False)
    monkeypatch.setattr(server, "_think_option_supported", lambda model: False)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 4096)
    return sent


def refused(call):
    with pytest.raises(server.ModelCallError) as caught:
        call()
    error = caught.value
    assert (error.kind, error.status, error.attempts) == ("configuration", 403, 0)
    assert getattr(error, "tool_lane_refused", False) is True
    return error.detail


# ---- Ollama: by name, by tier, by the policy ----------------------------------

@pytest.mark.parametrize("name", SPELLINGS)
def test_a_denied_model_is_refused_before_anything_is_sent(posts, name):
    gen = server._make_generate(name, "", 0.2, 64, 4096)
    assert refused(lambda: gen("hi")) == BLOCKED % name
    assert posts == []


@pytest.mark.parametrize("name", SPELLINGS)
def test_a_text_only_call_still_reaches_the_denied_model(posts, name):
    gen = server._make_generate(name, "", 0.2, 64, 4096, text_only=True)
    assert gen("hi") == "answer from " + name
    assert [(path, payload["model"]) for path, payload, _ in posts] == [("/api/chat", name)]


def test_tier_generation_refuses_by_default_and_answers_when_text_only(posts):
    gen = server._make_tier_generate("code", DENIED, "", 0.2, 64, 4096)
    assert refused(lambda: gen("hi")) == BLOCKED % DENIED
    assert posts == []
    text = server._make_tier_generate("code", DENIED, "", 0.2, 64, 4096, text_only=True)
    assert text("hi") == "answer from " + DENIED
    assert len(posts) == 1


def test_generate_text_follows_its_flag(posts, monkeypatch):
    monkeypatch.setitem(server.TIERS, "fast", DENIED)  # what a stray tier binding would do
    assert refused(lambda: server._generate_text("hi", tier="fast", num_ctx=4096)) == BLOCKED % DENIED
    assert posts == []
    assert server._generate_text("hi", tier="fast", num_ctx=4096, text_only=True) == "answer from " + DENIED
    assert len(posts) == 1


def test_internal_title_and_summary_generation_is_text_only(posts, monkeypatch):
    monkeypatch.setitem(server.TIERS, "fast", DENIED)
    generate = server._internal_generate_for_route(None, False)
    assert generate("summarize", num_ctx=4096) == "answer from " + DENIED
    assert len(posts) == 1


def test_a_name_the_policy_adds_is_refused(posts, isolated):
    write_policy(isolated, {"tool_lane_denied_models": {"names": ["extra-model:7b"]}})
    gen = server._make_generate("extra-model:7b", "", 0.2, 64, 4096)
    assert refused(lambda: gen("hi")) == BLOCKED % "extra-model:7b"
    assert posts == []
    assert server._make_generate("extra-model:7b", "", 0.2, 64, 4096, text_only=True)("hi")


def test_an_unreadable_policy_refuses_the_built_in_list_but_not_an_ordinary_model(posts, isolated):
    write_policy(isolated, "{not json")
    assert server._make_generate(ALLOWED, "", 0.2, 64, 4096)("hi") == "answer from " + ALLOWED
    gen = server._make_generate(DENIED, "", 0.2, 64, 4096)
    assert refused(lambda: gen("hi")) == BLOCKED % DENIED
    assert [payload["model"] for _, payload, _ in posts] == [ALLOWED]


def test_a_payload_naming_a_denied_model_is_refused_whatever_the_routing_name(posts):
    payload = {"model": DENIED, "messages": [{"role": "user", "content": "hi"}], "options": {}}
    assert refused(lambda: server._chat_request(payload, model=ALLOWED)) == BLOCKED % DENIED
    assert posts == []


def test_every_continuation_and_retry_keeps_the_text_only_declaration(posts, monkeypatch):
    """The budget retry re-enters _chat_request: a dropped flag would refuse it."""
    replies = iter([
        {"model": DENIED, "message": {"role": "assistant", "content": "", "thinking": "t" * 50},
         "done_reason": "length", "eval_count": 64},
        {"model": DENIED, "message": {"role": "assistant", "content": "final"}, "done_reason": "stop"},
    ])

    def fake_post(path, payload, **kwargs):
        posts.append((path, copy.deepcopy(payload), dict(kwargs)))
        return next(replies), 1

    monkeypatch.setattr(server, "_post_model", fake_post)
    payload = {"model": DENIED, "messages": [{"role": "user", "content": "hi"}],
               "options": {"num_predict": 64}}
    out, content = server._chat_request(payload, model=DENIED, text_only=True)
    assert content == "final" and len(posts) == 2


# ---- the hosted K3 fallback is judged again ------------------------------------

def _k3_then(posts, monkeypatch, fallback):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(server, "CLOUD_EXTRA_USAGE_FALLBACK_MODEL", fallback)

    def fake_post(path, payload, **kwargs):
        posts.append((path, copy.deepcopy(payload), dict(kwargs)))
        if payload["model"].startswith("kimi-k3:"):
            raise server.ModelCallError("request", "extra usage required", status=402, cloud=True)
        return {"message": {"role": "assistant", "content": "answer from " + payload["model"]}}, 1

    monkeypatch.setattr(server, "_post_model", fake_post)
    return {"model": "kimi-k3:cloud", "messages": [{"role": "user", "content": "hi"}], "options": {}}


def test_the_cloud_fallback_model_is_judged_again(posts, monkeypatch):
    payload = _k3_then(posts, monkeypatch, "astra-cyber-27b:cloud")
    detail = refused(lambda: server._chat_request_with_cloud_fallback(payload, model="kimi-k3:cloud"))
    assert detail == BLOCKED % "astra-cyber-27b:cloud"
    assert [sent["model"] for _, sent, _ in posts] == ["kimi-k3:cloud"]  # the fallback was never sent


def test_a_text_only_cloud_call_keeps_its_declaration_through_the_fallback(posts, monkeypatch):
    payload = _k3_then(posts, monkeypatch, "astra-cyber-27b:cloud")
    out, content, used = server._chat_request_with_cloud_fallback(
        payload, model="kimi-k3:cloud", text_only=True)
    assert (content, used) == ("answer from astra-cyber-27b:cloud", "astra-cyber-27b:cloud")


def test_an_ordinary_cloud_fallback_is_unchanged(posts, monkeypatch):
    payload = _k3_then(posts, monkeypatch, "kimi-k2.7:cloud")
    out, content, used = server._chat_request_with_cloud_fallback(payload, model="kimi-k3:cloud")
    assert used == "kimi-k2.7:cloud"
    assert [sent["model"] for _, sent, _ in posts] == ["kimi-k3:cloud", "kimi-k2.7:cloud"]


# ---- bridged rungs: the configured served model, before sending ----------------

BRIDGES = (
    pytest.param("sonder-inference", "sonder_inference",
                 {"SONDER_INFERENCE_TIER_MODELS": "code=astra-cyber-9b"}, "astra-cyber-9b", id="sonder-inference"),
    pytest.param("openrouter", "openrouter",
                 {"SONDER_OPENROUTER_TIER_MODELS": "code=acme/astra-cyber-27b", "SONDER_ALLOW_CLOUD": "1"},
                 "acme/astra-cyber-27b", id="openrouter"),
    pytest.param("openai_compatible", "openai_compatible",
                 {"SONDER_OPENAI_MODEL": DENIED}, DENIED, id="openai-compatible"),
)


class Gateway:
    """The packaged gateway a bridged rung is served by; reports ``served``."""

    def __init__(self, served="ordinary-served:8b"):
        self.calls = []
        self.served = served

    def generate(self, request, context):
        self.calls.append(request)
        return ModelResponse(text="bridged answer", model=self.served, tier=request.tier, tokens_out=1)


def bridged(monkeypatch, provider, env, served="ordinary-served:8b"):
    monkeypatch.setenv("SONDER_CODE_PROVIDER", provider)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    gateway = Gateway(served)
    monkeypatch.setattr(server, "_APP_GRAPH", SimpleNamespace(
        provider_bindings=provider_bindings_from_env(), model_gateway=gateway))
    return gateway


@pytest.mark.parametrize("provider, label, env, served", BRIDGES)
def test_a_bridged_rung_whose_served_model_is_denied_is_refused_before_sending(
        posts, monkeypatch, provider, label, env, served):
    gateway = bridged(monkeypatch, provider, env)
    gen = server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0)
    assert refused(lambda: gen("hi")) == SERVED % ("code", label, served)
    assert gateway.calls == [] and posts == []


@pytest.mark.parametrize("provider, label, env, served", BRIDGES)
def test_a_bridged_rung_whose_served_model_is_denied_still_answers_text(
        posts, monkeypatch, provider, label, env, served):
    gateway = bridged(monkeypatch, provider, env)
    assert server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0, text_only=True)("hi") == "bridged answer"
    assert len(gateway.calls) == 1 and posts == []


def test_a_bridged_rung_whose_served_model_is_ordinary_is_served(posts, monkeypatch):
    gateway = bridged(monkeypatch, "sonder-inference", {"SONDER_INFERENCE_TIER_MODELS": "code=" + ALLOWED},
                      served=ALLOWED)
    assert server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0)("hi") == "bridged answer"
    assert len(gateway.calls) == 1


# ---- bridged rungs: the served model the reply reports -----------------------

@pytest.mark.parametrize("reported", ["astra-cyber-9b", "Astra-Cyber-27B:latest", "acme/astra-cyber-27b", HF_ORNITH])
def test_a_reply_reporting_a_denied_served_model_is_refused(posts, monkeypatch, reported):
    gateway = bridged(monkeypatch, "sonder-inference", {"SONDER_INFERENCE_TIER_MODELS": "code=" + ALLOWED},
                      served=reported)
    gen = server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0)
    assert refused(lambda: gen("hi")) == SERVED % ("code", "sonder_inference", reported)
    assert len(gateway.calls) == 1  # judged after the provider answered; the reply is dropped


@pytest.mark.parametrize("reported", ["", "default", "code", "sonder"])
def test_a_reply_that_names_no_model_is_refused(posts, monkeypatch, reported):
    gateway = bridged(monkeypatch, "openai_compatible", {}, served=reported)
    gen = server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0)
    assert refused(lambda: gen("hi")) == UNKNOWN_OPENAI % "code"  # names the setting that fixes it
    assert len(gateway.calls) == 1  # nothing named before sending, so the reply decides


def test_a_text_only_bridged_call_answers_whatever_the_reply_reports(posts, monkeypatch):
    gateway = bridged(monkeypatch, "openai_compatible", {}, served="astra-cyber-9b")
    assert server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0, text_only=True)("hi") == "bridged answer"
    assert len(gateway.calls) == 1


def test_a_reply_reporting_an_ordinary_served_model_is_returned(posts, monkeypatch):
    gateway = bridged(monkeypatch, "openai_compatible", {}, served="vendor/ordinary-7b")
    assert server._make_tier_generate("code", ALLOWED, "", 0.2, 64, 0)("hi") == "bridged answer"
    assert len(gateway.calls) == 1


def test_require_served_unit():
    rung = SimpleNamespace(provider="sonder_inference", tier="code")
    guard.require_served(rung, "ordinary:8b")
    guard.require_served(rung, DENIED, text_only=True)
    for reported in (DENIED, "", "default", "general", None):
        with pytest.raises(server.ModelCallError):
            guard.require_served(rung, reported)


# ---- ordinary models: nothing changes ------------------------------------------

def test_an_ordinary_models_request_is_the_same_whether_declared_text_only_or_not(posts):
    server._make_generate(ALLOWED, "sys", 0.2, 64, 4096)("hi", [{"role": "assistant", "content": "a"}])
    server._make_generate(ALLOWED, "sys", 0.2, 64, 4096, text_only=True)(
        "hi", [{"role": "assistant", "content": "a"}])
    assert len(posts) == 2 and posts[0] == posts[1]
    path, payload, kwargs = posts[0]
    assert path == "/api/chat" and payload["model"] == ALLOWED
    assert "text_only" not in payload and "text_only" not in kwargs


def test_the_deny_list_is_read_once_per_version_of_the_policy_file(posts, isolated, monkeypatch):
    write_policy(isolated, {"tool_lane_denied_models": {"names": ["extra-model:7b"]}})
    loads = []
    real_load = runtime_policy.load
    monkeypatch.setattr(runtime_policy, "load", lambda *a, **k: loads.append(1) or real_load(*a, **k))
    for _ in range(3):
        server._make_generate(ALLOWED, "", 0.2, 64, 4096)("hi")
    assert len(loads) == 1
    write_policy(isolated, {"tool_lane_denied_models": {"names": ["extra-model:7b", "other-model:1b"]}})
    gen = server._make_generate("other-model:1b", "", 0.2, 64, 4096)
    assert refused(lambda: gen("hi")) == BLOCKED % "other-model:1b"  # the edit is seen
    assert len(loads) == 2


def test_a_failed_policy_read_is_not_cached(posts, isolated, monkeypatch):
    write_policy(isolated, "{not json")
    loads = []
    real_load = runtime_policy.load
    monkeypatch.setattr(runtime_policy, "load", lambda *a, **k: loads.append(1) or real_load(*a, **k))
    for _ in range(2):
        server._make_generate(ALLOWED, "", 0.2, 64, 4096)("hi")
    assert len(loads) == 2
    assert len(posts) == 2


def test_a_text_only_call_reads_no_policy(posts, monkeypatch):
    monkeypatch.setattr(runtime_policy, "load", lambda *a, **k: pytest.fail("policy read on a text-only call"))
    assert server._make_generate(DENIED, "", 0.2, 64, 4096, text_only=True)("hi") == "answer from " + DENIED


def test_require_generation_unit():
    guard.require_generation(DENIED, text_only=True)
    guard.require_generation(ALLOWED)
    guard.require_generation(ALLOWED, payload={"model": ALLOWED})
    with pytest.raises(server.ModelCallError):
        guard.require_generation(ALLOWED, payload={"model": DENIED})
    with pytest.raises(server.ModelCallError):
        guard.require_generation("library/astra-cyber-27b")


# ---- text lanes keep answering a denied model, through the real stack ----------
#
# A text lane that failed to forward text_only would be refused here: each of
# these goes through the real _make_generate / _chat_request with only the
# transport faked.

@pytest.fixture
def chat(monkeypatch, posts):
    names = {name.strip().casefold(): name for name in SPELLINGS + (ALLOWED,)}

    def record(selector):
        key = str(selector or "").strip().casefold()
        return (names[key], {"name": names[key]}) if key in names else None

    monkeypatch.setattr(server, "resolve_discovered_model_record", record)
    monkeypatch.setattr(server, "_fanout_nonchat_reason", lambda record: "")
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setenv("SONDER_MODEL_ESCALATION", "0")
    return posts


def models(posts):
    return [payload["model"] for _path, payload, _kwargs in posts]


@pytest.mark.parametrize("name", [DENIED, HF_QWEN])
def test_mcp_chat_by_name_answers(chat, name):
    out = server._sonder_impl("hi", tier=name, session="none", project="none")
    assert out.startswith("answer from " + name) and models(chat) == [name]


def test_mcp_answer_with_history_by_name_answers(chat):
    out = server.answer_with_history("hi", [], tier=DENIED, raise_model_errors=True)
    assert out.startswith("answer from " + DENIED) and models(chat) == [DENIED]


def test_mcp_chat_by_tier_answers_on_a_tier_serving_a_denied_model(chat, monkeypatch):
    gateway = bridged(monkeypatch, "sonder-inference", {"SONDER_INFERENCE_TIER_MODELS": "code=astra-cyber-9b"},
                      served="astra-cyber-9b")
    out = server._answer_with_history_impl("hi", [], tier="code", raise_model_errors=True, augment=False)
    assert "bridged answer" in out and len(gateway.calls) == 1 and chat == []


def test_http_chat_by_name_answers(chat):
    import sonder_runtime.interfaces.http.serve as serve

    out = serve._run_prompt("hi", [], tier=DENIED, session="none", project="none")
    assert str(out).startswith("answer from " + DENIED) and models(chat) == [DENIED]


def test_structured_answer_by_name_answers(chat, monkeypatch):
    def fake_post(path, payload, **kwargs):
        chat.append((path, copy.deepcopy(payload), dict(kwargs)))
        return {"model": payload["model"], "message": {"role": "assistant", "content": '{"ok": true}'}}, 1

    monkeypatch.setattr(server, "_post_model", fake_post)
    out = server.structured_answer_with_history("hi", [], {"type": "object"}, tier=DENIED)
    assert '"ok"' in out and models(chat) == [DENIED]


def test_ensemble_answer_asks_a_named_denied_model(chat):
    out = server.ensemble_answer("hi", tiers=DENIED)
    assert out.startswith("answer from " + DENIED) and models(chat) == [DENIED]


def test_consult_asks_a_named_denied_model(chat):
    out = server.consult("hi", tiers=DENIED + "," + ALLOWED)
    assert "answer from " + DENIED in out and DENIED in models(chat) and ALLOWED in models(chat)


@pytest.mark.parametrize("learn", [False, True], ids=["nonlearning", "learning"])
def test_offload_answers_on_a_tier_bound_to_a_denied_model(chat, monkeypatch, learn):
    monkeypatch.setitem(server.TIERS, "fast", DENIED)
    out = server._offload_impl("hi", tier="fast", learn=learn, num_ctx=4096)
    assert out.startswith("answer from " + DENIED) and models(chat) == [DENIED]


def test_vision_answers_with_a_denied_vision_model(chat, monkeypatch, tmp_path):
    import base64
    import hashlib

    image = tmp_path / "sample.png"
    image.write_bytes(base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVQIHWP4z8DwHwAFgAI/ScL5XQAAAABJRU5ErkJggg=="))
    vision = "astra-cyber-vl:3b"
    monkeypatch.setitem(server.TIERS, "vision", vision)
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda _base: True)
    monkeypatch.setattr(server.context_policy, "native", lambda *_args: 4000)
    monkeypatch.setattr(server, "_runtime_installed_model_records",
                        lambda: ((vision, {"capabilities": ["vision", "completion"]}),))
    monkeypatch.setattr(server.file_ops, "require_read_access", lambda *_a, **_k: image)
    monkeypatch.setattr(server.workbench, "image_inspect", lambda *_a, **_k: {
        "format": "PNG", "bytes": image.stat().st_size, "width": 1, "height": 1,
        "sha256": hashlib.sha256(image.read_bytes()).hexdigest()})
    assert server._vision_analyze_impl(str(image), "what is it?") == "answer from " + vision
    assert models(chat) == [vision]


def test_a_fanout_row_for_a_denied_model_answers(chat, monkeypatch, tmp_path):
    """The sweep normally skips a denied model; a row that names one is still text."""
    monkeypatch.setattr(server.fanout_store, "database_path", lambda: str(tmp_path / "fanout.db"))
    monkeypatch.setenv("SONDER_FANOUT_KEY_FILE", str(tmp_path / "fanout.key"))
    server.fanout_store.reset_schema_cache_for_tests()
    server.fanout_prompt_vault.reset_cache_for_tests()
    monkeypatch.setattr(server, "_get", lambda path: (
        {"models": [{"name": DENIED}]} if path == "/api/tags" else {"models": []}))
    monkeypatch.setattr(server, "_post", lambda path, payload, **kwargs: {})
    monkeypatch.setattr(server.fanout_store, "get_model_health", lambda _name: None)
    monkeypatch.setattr(guard, "sweep_skip_reason", lambda *a, **k: "")
    run = server._fanout_start("hello", "", cap=4, request_timeout=5, cloud_workers=1)
    receipt = server._execute_fanout_run(run["id"])
    assert receipt["status"] == "completed" and receipt["failures"] == []
    assert [row["answer"] for row in receipt["answers"]] == ["answer from " + DENIED]
