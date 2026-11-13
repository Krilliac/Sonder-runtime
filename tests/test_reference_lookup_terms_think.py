"""The reference lookup's search-terms call runs with model thinking off.

The reply is one line of query words.  Left to reason, a thinking model spent
the call's 80 tokens on thought, was retried with the local thinking budget and
could use that up too (measured live: two 4096-token thoughts, no terms).  The
single-model lanes and the ensemble build the call through one helper, which
asks again the old way when the no-thinking reply is lost to the option itself.
"""
from __future__ import annotations

import ast
import json

import pytest

import server
import sonder_runtime.adapters.reference_lookup_lanes as lanes
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.domain.common.errors import Cancelled
from tests.support import generation_call_sites as call_sites
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


def _stubborn_thinking_model(lane):  # noqa: F811
    """A reasoning model whose route ignores ``think``: a small reply is all thought either way."""
    answer = lane.__call__

    def call(path, payload, *args, **kwargs):
        if path != "/api/chat" or not _is_search_terms(payload):
            return answer(path, payload, *args, **kwargs)
        lane.payloads.append(json.loads(json.dumps(payload)))
        if payload["options"]["num_predict"] < server.LOCAL_THINKING_MIN_NUM_PREDICT:
            return {"message": {"content": "", "thinking": "considering " * 20},
                    "done_reason": "length", "eval_count": payload["options"]["num_predict"]}
        return {"message": {"content": TERMS, "thinking": "done"}, "done_reason": "stop"}

    return call


@pytest.mark.parametrize("ask", [_single, _ensemble], ids=["single", "ensemble"])
def test_model_that_thinks_anyway_is_asked_again_as_before(lane, monkeypatch, ask):  # noqa: F811
    monkeypatch.setattr(server, "_post", _stubborn_thinking_model(lane))
    monkeypatch.setattr(server, "_THINKING_CAPABILITY_CACHE", {})

    out = ask(monkeypatch)

    # The empty reply showed the runtime a thinking model, so the second request
    # is the call as it was for one: thinking left alone, the thinking budget.
    calls = [(p.get("think"), p["options"]["num_predict"]) for p in _search_terms_calls(lane)]
    assert calls == [(False, 80), (None, server.LOCAL_THINKING_MIN_NUM_PREDICT)]
    assert "keyword expansion" not in out and "Sources:" in out


class _Factory:
    """Records how each generator was built.

    ``hosted`` refuses ``think`` when a call is built, like a hosted target;
    ``quick_error`` is raised by a generator that was built with thinking off.
    """

    def __init__(self, hosted=False, quick_error=None):
        self.hosted = hosted
        self.quick_error = quick_error
        self.built = []
        self.asked = []

    def __call__(self, *args, **options):
        if self.hosted and options.get("think") is not None:
            raise ValueError("hosted-model thinking is controlled by provider policy")
        self.built.append((args, options))

        def generate(prompt, *a, **k):
            self.asked.append(options.get("think"))
            if self.quick_error is not None and options.get("think") is False:
                raise self.quick_error
            return TERMS

        return generate


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
    assert factory.asked == []  # building asks nothing


def test_callers_own_thinking_choices_do_not_reach_the_search_terms_call():
    factory = _Factory()

    lanes.terms_generate(factory, LOOKUP_MODEL, 8192, think=True, reasoning_continuation=True,
                         reasoning_total_tokens=9000)

    ((_args, options),) = factory.built
    assert options == {"text_only": True, "think": False}


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


@pytest.mark.parametrize("error", [
    ModelCallError("empty_response", "Ollama returned no assistant content"),
    ModelCallError("http", "model does not support thinking", status=400),
], ids=["empty_reply", "option_refused"])
def test_a_reply_lost_to_the_option_is_asked_again_as_the_plain_call(error):
    factory = _Factory(quick_error=error)

    reply = lanes.terms_generate(factory, LOOKUP_MODEL, 8192, think=True)("prompt")

    assert reply == TERMS
    assert [options for _args, options in factory.built] == [
        {"text_only": True, "think": False},
        {"text_only": True, "think": True},  # the caller's own options, as the call was before
    ]
    assert factory.asked == [False, True]


@pytest.mark.parametrize("error", [
    ModelCallError("timeout", "model call timed out"),
    ModelCallError("http", "service unavailable", status=503, transient=True),
    ModelCallError("cancelled", "the caller cancelled the turn"),
    Cancelled("the caller cancelled the turn"),
    RuntimeError("anything else"),
], ids=["timeout", "unavailable", "cancelled_call", "cancelled", "other"])
def test_any_other_failure_is_raised_and_not_asked_again(error):
    factory = _Factory(quick_error=error)

    with pytest.raises(type(error)):
        lanes.terms_generate(factory, LOOKUP_MODEL, 8192)("prompt")

    assert factory.asked == [False]
    assert len(factory.built) == 1


def _terms_generate_references():
    """Every place outside the tests that names ``terms_generate``, as (path, enclosing scope)."""
    found = set()

    def walk(node, scope, path):
        for child in ast.iter_child_nodes(node):
            named = (
                (isinstance(child, ast.Name) and child.id == "terms_generate")
                or (isinstance(child, ast.Attribute) and child.attr == "terms_generate")
                or (isinstance(child, ast.alias) and child.name == "terms_generate")
                or (isinstance(child, ast.Constant) and child.value == "terms_generate")
            )
            if named:
                found.add((path, ".".join(scope) or "<module>"))
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                inner = (*scope, child.name)
            walk(child, inner, path)

    scanned = 0
    for _label, pattern, _floor in call_sites.ROOTS:
        for source in call_sites.REPO_ROOT.glob(pattern):
            relative = source.relative_to(call_sites.REPO_ROOT)
            if not source.is_file() or "__pycache__" in source.parts or relative.parts[0] == "tests":
                continue
            scanned += 1
            walk(ast.parse(source.read_text(encoding="utf-8-sig")), (), relative.as_posix())
    assert scanned > 1000, "the scan is not seeing the tree"
    return found


def test_only_the_two_lookup_seams_use_the_text_only_helper():
    """``terms_generate`` declares its call text-only on its caller's behalf.

    The generation call-site ratchet registers the helper, not its callers, so a
    new caller would be text-only without being classified.  The reply of each
    caller below is only parsed into query words; classify a new one the same way
    (tests/support/generation_call_sites.py) before adding it here.
    """
    lanes_path = "sonder_runtime/adapters/reference_lookup_lanes.py"
    assert _terms_generate_references() == {
        (lanes_path, "<module>"),                 # the ``__all__`` export
        (lanes_path, "chat_generate.expander"),   # single-model chat lanes
        ("sonder_runtime/adapters/reference_lookup_multi.py", "SharedLookup.retrieve.expand"),
    }
