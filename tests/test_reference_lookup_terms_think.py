"""The reference lookup's search-terms call runs with model thinking off.

The reply is one line of query words.  Left to reason, a thinking model spent
the call's 80 tokens on thought, was retried with the local thinking budget and
could use that up too (measured live: two 4096-token thoughts, no terms).  The
single-model lanes and the ensemble build the call through one helper.
"""
from __future__ import annotations

import json

import pytest

import server
import sonder_runtime.adapters.reference_lookup_lanes as lanes
from tests.test_reference_lookup_lanes import (  # noqa: F401  # ``lane`` is a fixture
    LOOKUP_MODEL,
    QUESTION,
    _forget_rule_cache,
    _mcp_by_name,
    lane,
)

TERMS = "failed logon, 4625"


def _is_search_terms(payload):
    return "choosing search terms" in payload["messages"][-1]["content"]


def _thinking_model(lane):  # noqa: F811  # the fixture's value, passed in
    """The lane's fake, answering like a local reasoning model.

    With thinking on, a reply capped below the local thinking budget is all
    thought and no content; with thinking off the content comes at once.
    """
    answer = lane.__call__

    def call(path, payload, *args, **kwargs):
        if path != "/api/chat" or not _is_search_terms(payload):
            return answer(path, payload, *args, **kwargs)
        lane.payloads.append(json.loads(json.dumps(payload)))
        if payload.get("think") is False:
            return {"message": {"content": TERMS}, "done_reason": "stop"}
        if payload["options"]["num_predict"] < server.LOCAL_THINKING_MIN_NUM_PREDICT:
            return {"message": {"content": "", "thinking": "considering " * 20},
                    "done_reason": "length", "eval_count": payload["options"]["num_predict"]}
        return {"message": {"content": TERMS, "thinking": "done"}, "done_reason": "stop"}

    return call


def _search_terms_calls(lane):  # noqa: F811
    return [p for p in lane.payloads if _is_search_terms(p)]


def _single(monkeypatch):
    return _mcp_by_name(monkeypatch, LOOKUP_MODEL)


def _ensemble(monkeypatch):
    monkeypatch.setattr(server, "_auto_model_context", lambda model: 8192)
    return server.ensemble_answer(QUESTION, tiers=LOOKUP_MODEL)


@pytest.mark.parametrize("ask", [_single, _ensemble], ids=["single", "ensemble"])
@pytest.mark.parametrize("known_thinking", [False, True], ids=["first_seen", "known_thinking"])
def test_search_terms_call_is_one_small_request_with_thinking_off(lane, monkeypatch, ask, known_thinking):  # noqa: F811
    monkeypatch.setattr(server, "_post", _thinking_model(lane))
    monkeypatch.setattr(
        server, "_THINKING_CAPABILITY_CACHE",
        {LOOKUP_MODEL: True, LOOKUP_MODEL + ":latest": True} if known_thinking else {},
    )

    out = ask(monkeypatch)

    (terms_call,) = _search_terms_calls(lane)
    assert terms_call["think"] is False
    assert terms_call["options"]["num_predict"] == lanes.EXPAND_NUM_PREDICT
    assert "keyword expansion" not in out and "Sources:" in out


def test_answer_call_is_not_told_to_stop_thinking(lane, monkeypatch):  # noqa: F811
    _single(monkeypatch)

    answers = [p for p in lane.payloads if not _is_search_terms(p)]
    assert answers and all(p.get("think") is not False for p in answers)


class _Factory:
    """Records how each generator was built; refuses ``think`` like a hosted target when told to."""

    def __init__(self, hosted=False):
        self.hosted = hosted
        self.built = []

    def __call__(self, *args, **options):
        if self.hosted and options.get("think") is not None:
            raise ValueError("hosted-model thinking is controlled by provider policy")
        self.built.append((args, options))
        return lambda prompt, *a, **k: TERMS


def test_helper_builds_the_bare_small_call_with_thinking_off():
    factory = _Factory()

    lanes.terms_generate(factory, LOOKUP_MODEL, 8192, cloud=False)
    lanes.terms_generate(factory, "general", LOOKUP_MODEL, 4096)  # a tier-bound factory

    assert factory.built == [
        ((LOOKUP_MODEL, "", lanes.EXPAND_TEMPERATURE, lanes.EXPAND_NUM_PREDICT, 8192),
         {"cloud": False, "text_only": True, "think": False}),
        (("general", LOOKUP_MODEL, "", lanes.EXPAND_TEMPERATURE, lanes.EXPAND_NUM_PREDICT, 4096),
         {"text_only": True, "think": False}),
    ]


def test_callers_own_thinking_choice_does_not_reach_the_search_terms_call():
    factory = _Factory()

    lanes.terms_generate(factory, LOOKUP_MODEL, 8192, think=True)

    ((_args, options),) = factory.built
    assert options["think"] is False


def test_hosted_target_gets_the_plain_call():
    factory = _Factory(hosted=True)

    generate = lanes.terms_generate(factory, LOOKUP_MODEL, 8192, cloud=True)

    assert generate("prompt") == TERMS
    ((_args, options),) = factory.built
    assert options == {"cloud": True, "text_only": True}


def test_the_runtime_refuses_the_option_for_a_hosted_model_when_the_call_is_built():
    """The contract the fallback above relies on: a refusal at build time, before any request."""
    with pytest.raises(ValueError, match="thinking"):
        server._make_generate(LOOKUP_MODEL, "", 0.0, 80, 4096, cloud=True, think=False)
