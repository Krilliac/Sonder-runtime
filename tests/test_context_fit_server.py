"""The HTTP chat loop applies context-fit routing and the per-tier Ollama window.

A turn whose estimated prompt does not fit its tier's model moves to the
long-context tier and the receipt says so; with no long-context tier it fails
before any model call with an actionable error (never a silent truncation).
A turn that fits runs exactly as before.
"""
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters.inference import launch_profile_cache
from sonder_runtime.application.routing import long_context_overflow as overflow

DENSE = "dense-32k:q4"
MOE = "moe-128k:q4"
LONG = [{"role": "user", "content": "x" * 160_000}]   # ~40k estimated tokens
CONTEXTS = {DENSE: 32768, MOE: 131072}


@pytest.fixture
def chat(monkeypatch):
    for name in ("SONDER_MODEL_BACKEND", "SONDER_FAST_PROVIDER", "SONDER_GENERAL_PROVIDER",
                 "SONDER_CODE_PROVIDER", "SONDER_REASONING_PROVIDER", "SONDER_VISION_PROVIDER",
                 "SONDER_LONG_CONTEXT_OVERFLOW", "SONDER_LONG_CONTEXT_MODEL",
                 "SONDER_LONG_CONTEXT_THRESHOLD", "SONDER_LONG_CONTEXT_TIER",
                 "SONDER_OLLAMA_TIER_OPTIONS", "SONDER_INFERENCE_TIER_MODELS"):
        monkeypatch.delenv(name, raising=False)
    launch_profile_cache.shared_cache().clear()
    monkeypatch.setitem(server.TIERS, "general", DENSE)
    monkeypatch.setitem(server.TIERS, "reasoning", MOE)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    # A live app graph left by an earlier test would win over the env bindings.
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    monkeypatch.setattr(server, "OLLAMA_POOL", SimpleNamespace(enabled=False, has_remote_workers=False))
    monkeypatch.setattr(server, "_RUNTIME_POLICY", runtime_policy.default_policy(env={}))
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: False)
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 16_384)
    monkeypatch.setattr(server, "_cache_model_revision", lambda model: "")
    monkeypatch.setattr(server, "_model_context_metadata",
                        lambda model: (CONTEXTS.get(model), None))
    seen = []

    def fake_make_generate(model, system, temperature, num_predict, num_ctx, **kwargs):
        def gen(prompt, history=None):
            seen.append({"model": model, "num_ctx": num_ctx})
            return "answer from " + model
        return gen

    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    yield monkeypatch, seen
    launch_profile_cache.shared_cache().clear()


def _turn(history, **kwargs):
    with overflow.notice_scope() as notes:
        reply = server._answer_with_history_impl("hello", history, tier="general", **kwargs)
    return reply, overflow.receipt_entry(notes)


def test_turn_that_fits_runs_exactly_as_before(chat):
    _monkeypatch, seen = chat
    reply, receipt = _turn([{"role": "user", "content": "short"}])
    assert reply.startswith("answer from " + DENSE)
    assert seen == [{"model": DENSE, "num_ctx": 16_384}]
    assert receipt is None


def test_too_long_turn_without_a_long_tier_fails_before_any_model_call(chat):
    _monkeypatch, seen = chat
    reply, receipt = _turn(LONG)
    assert seen == []
    assert reply.startswith("ERROR: this request is about 400"), reply[:200]
    assert "32768-token context" in reply and "nothing was truncated" in reply
    assert "long_context_tier" in reply
    assert receipt is None


def test_too_long_turn_moves_to_the_long_context_tier_and_says_so(chat):
    monkeypatch, seen = chat
    monkeypatch.setenv("SONDER_LONG_CONTEXT_TIER", "reasoning")
    reply, receipt = _turn(LONG)
    assert reply.startswith("answer from " + MOE)
    assert len(seen) == 1 and seen[0]["model"] == MOE
    assert 40_000 < seen[0]["num_ctx"] <= 131_072
    assert receipt["status"] == "switched" and receipt["route"] == "context_fit"
    assert receipt["notice"].startswith(
        "context fit: routed to long-context tier reasoning (moe-128k:q4)")


def test_tier_num_ctx_is_sent_for_the_tier(chat):
    monkeypatch, seen = chat
    monkeypatch.setenv("SONDER_OLLAMA_TIER_OPTIONS", '{"general": {"num_ctx": 24576}}')
    _turn([{"role": "user", "content": "short"}])
    assert seen[0]["num_ctx"] == 24_576
    seen.clear()
    _turn([{"role": "user", "content": "short"}], context_size="8k")
    assert seen[0]["num_ctx"] != 24_576  # an explicit window still wins
