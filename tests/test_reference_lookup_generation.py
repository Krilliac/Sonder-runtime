"""Reference lookup through the tool-lane generation check.

By default the lookup rule matches exactly the models the tool-lane deny list
names, and generation refuses a denied model unless the call site declared
itself text-only.  Here every lane runs through the real ``_make_generate``,
``_make_tier_generate`` and ``server._chat_request`` (whose
``tool_lane_guard.require_generation`` is the bottom check); only the Ollama
transport (``server._post``) is faked, by the lookup lanes' ``Transport``.

For a deny-listed lookup model each text lane (MCP chat by tier and by name,
HTTP chat by name, ``ensemble_answer``, ``consult``) answers with lookup
applied and makes exactly the expected model calls, each one judged by the
bottom check as text-only.  For the same model a lane that runs model output
(``parallel_generate_run``, ``_agent_turn``) is still refused, sends nothing
and makes no lookup call.
"""
from __future__ import annotations

import json

import pytest

import server
import sonder_runtime.adapters.reference_lookup_lanes as lanes
import sonder_runtime.adapters.reference_lookup_service as service
from sonder_runtime.adapters import tool_lane_guard as guard
from tests.support.tool_lane import BLOCKED, clear_environment
from tests.test_reference_lookup_lanes import (  # noqa: F401  # ``lane`` is a fixture
    ANSWER,
    LOOKUP_MODEL,
    MARKER_RE,
    PASSAGE_TEXT,
    PLAIN_MODEL,
    QUESTION,
    _bind_general,
    _chat_body,
    _mcp_by_name,
    _mcp_by_tier,
    _serve_http,
    lane,
)

SOURCE_URL = "https://example.test/events/4625"


@pytest.fixture
def checked(request, monkeypatch):
    """The lookup lane fixture with the deny list in force and every bottom check recorded.

    Yields ``(transport, judged, retrievals)``: the fake transport, one
    ``(model, text_only)`` per ``require_generation`` call, and one entry per
    reference retrieval.
    """
    transport = request.getfixturevalue("lane")
    clear_environment(monkeypatch)
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", server._RUNTIME_POLICY)
    server._refresh_runtime_policy(create=False)
    names = {name.casefold(): name for name in (LOOKUP_MODEL, PLAIN_MODEL)}
    monkeypatch.setattr(server, "resolve_discovered_model_record", lambda selector: (
        (names[key], {"name": names[key]})
        if (key := str(selector or "").strip().casefold()) in names else None))
    monkeypatch.setattr(server, "_fanout_nonchat_reason", lambda record: "")
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setenv("SONDER_MODEL_ESCALATION", "0")

    judged = []
    real_require = guard.require_generation

    def require(model, **kwargs):
        judged.append((str(model), bool(kwargs.get("text_only"))))
        return real_require(model, **kwargs)

    monkeypatch.setattr(guard, "require_generation", require)

    retrievals = []
    real_retrieve = service.retrieve_for

    def retrieve(question, expand_fn, k, char_budget):
        retrievals.append(question)
        return real_retrieve(question, expand_fn, k, char_budget)

    monkeypatch.setattr(service, "retrieve_for", retrieve)
    return transport, judged, retrievals


def _models(payloads):
    return [payload["model"] for payload in payloads]


def _assert_judged_text_only(judged, count):
    lookup_calls = [entry for entry in judged if entry[0] == LOOKUP_MODEL]
    assert lookup_calls == [(LOOKUP_MODEL, True)] * count, judged


def _assert_grounded(text):
    assert ANSWER in text
    assert "Sources:" in text and SOURCE_URL in text
    assert PASSAGE_TEXT not in text and not MARKER_RE.search(text)


# --- the premise: the model is deny-listed and lookup applies to it -----------


def test_the_lookup_model_is_deny_listed_and_refused_when_undeclared(checked):
    transport, judged, _retrievals = checked
    assert service.applies(LOOKUP_MODEL).applies is True
    assert guard.deny_list(None).readable
    with pytest.raises(server.ModelCallError) as caught:
        server._make_generate(LOOKUP_MODEL, "", 0.2, 64, 4096)("hi")
    error = caught.value
    assert (error.kind, error.status, error.attempts) == ("configuration", 403, 0)
    assert getattr(error, "tool_lane_refused", False) is True
    assert transport.payloads == []
    assert judged == [(LOOKUP_MODEL, False)]


# --- text lanes: lookup applies and every call is judged text-only -------------


@pytest.mark.parametrize("call", (_mcp_by_tier, _mcp_by_name), ids=("mcp_by_tier", "mcp_by_name"))
def test_mcp_chat_answers_with_lookup_on_a_deny_listed_model(checked, monkeypatch, call):
    transport, judged, retrievals = checked
    out = call(monkeypatch, LOOKUP_MODEL)

    _assert_grounded(out)
    assert retrievals == [QUESTION]
    assert _models(transport.expansions()) == [LOOKUP_MODEL]
    assert _models(transport.answers()) == [LOOKUP_MODEL]
    assert PASSAGE_TEXT in transport.user_turn()
    _assert_judged_text_only(judged, 2)


def test_http_chat_by_name_answers_with_lookup_on_a_deny_listed_model(checked, monkeypatch, tmp_path):
    from tests.test_serve_auth import _http_server, _request

    transport, judged, retrievals = checked
    _serve_http(monkeypatch, tmp_path, learning=False)
    _bind_general(monkeypatch, PLAIN_MODEL)  # the request names the model, not the tier
    with _http_server(monkeypatch) as port:
        status, _headers, payload = _request(
            port, "POST", "/v1/chat/completions", body=_chat_body("G1", model=LOOKUP_MODEL),
            headers={"Content-Type": "application/json"})
    assert status == 200, payload
    reply = json.loads(payload)

    _assert_grounded(reply["choices"][0]["message"]["content"])
    assert reply["sonder_receipt"]["reference_lookup"]["applied"] is True
    assert retrievals == [QUESTION]
    assert _models(transport.expansions()) == [LOOKUP_MODEL]
    assert _models(transport.answers()) == [LOOKUP_MODEL]
    _assert_judged_text_only(judged, 2)


def test_ensemble_answer_answers_with_lookup_on_a_deny_listed_model(checked):
    transport, judged, retrievals = checked
    out = server.ensemble_answer(QUESTION, tiers=PLAIN_MODEL + "," + LOOKUP_MODEL)

    assert "synthesis failed" not in out and "refused" not in out.lower()
    assert "Sources:" in out and SOURCE_URL in out
    assert retrievals == [QUESTION]
    # Search terms once (from the lookup member), both members framed alike,
    # then the synthesis on the last member's model.
    assert _models(transport.expansions()) == [LOOKUP_MODEL]
    members = transport.answers()[:2]
    assert sorted(_models(members)) == sorted([PLAIN_MODEL, LOOKUP_MODEL])
    assert all(PASSAGE_TEXT in payload["messages"][-1]["content"] for payload in members)
    assert _models(transport.answers()[2:]) == [LOOKUP_MODEL]
    _assert_judged_text_only(judged, 3)
    assert (PLAIN_MODEL, True) in judged


def test_consult_answers_with_lookup_on_a_deny_listed_model(checked):
    transport, judged, retrievals = checked
    out = server.consult(QUESTION, tiers=LOOKUP_MODEL + "," + PLAIN_MODEL)

    assert ANSWER in out and "Sources:" in out and SOURCE_URL in out
    assert "refused" not in out.lower()
    assert retrievals == [QUESTION]
    assert _models(transport.expansions()) == [LOOKUP_MODEL]
    framed = [p for p in transport.answers() if PASSAGE_TEXT in p["messages"][-1]["content"]]
    assert sorted(_models(framed)) == sorted([LOOKUP_MODEL, PLAIN_MODEL])
    # The judge is asked without passages.
    unframed = [p for p in transport.answers() if PASSAGE_TEXT not in p["messages"][-1]["content"]]
    assert len(unframed) == 1
    lookup_calls = sum(1 for p in transport.payloads if p["model"] == LOOKUP_MODEL)
    _assert_judged_text_only(judged, lookup_calls)
    assert lookup_calls >= 2


# --- lanes that run model output: still refused, no lookup call ----------------


def test_parallel_generate_run_refuses_the_lookup_model_and_looks_nothing_up(checked, monkeypatch):
    transport, judged, retrievals = checked
    monkeypatch.setitem(server.TIERS, "code", LOOKUP_MODEL)
    executed = []
    monkeypatch.setattr(server.grounding, "run_code", lambda code, *a, **k: (executed.append(code), (True, "ok"))[1])

    out = server.parallel_generate_run("double a number", variants=2, max_workers=1, tier="code")

    assert out == BLOCKED % LOOKUP_MODEL
    assert transport.payloads == [] and executed == []
    assert retrievals == []
    assert judged == []


def test_agent_turn_refuses_the_lookup_model_and_looks_nothing_up(checked):
    transport, judged, retrievals = checked
    assert server._agent_turn("list files", tier=LOOKUP_MODEL) == BLOCKED % LOOKUP_MODEL
    assert transport.payloads == []
    assert retrievals == []
    assert judged == []


def test_the_lookup_seams_never_bind_an_exec_lane(checked, monkeypatch):
    """The EXEC lanes reach no lookup seam: no record is created for their model."""
    _transport, _judged, _retrievals = checked
    seen = []
    real_record = lanes._record
    monkeypatch.setattr(lanes, "_record", lambda model: (seen.append(model), real_record(model))[1])
    monkeypatch.setitem(server.TIERS, "code", LOOKUP_MODEL)
    server.parallel_generate_run("double a number", variants=1, max_workers=1, tier="code")
    server._agent_turn("list files", tier=LOOKUP_MODEL)
    assert seen == []


def test_an_undeclared_lane_gets_no_lookup_and_sends_nothing(checked, monkeypatch):
    """A lane that did not declare itself text-only is called through: no record, no search terms."""
    transport, judged, retrievals = checked
    seen = []
    real_record = lanes._record
    monkeypatch.setattr(lanes, "_record", lambda model: (seen.append(model), real_record(model))[1])

    gen = lanes.chat_generate(server._make_generate, QUESTION, LOOKUP_MODEL, "", 0.2, 1024, 4096)
    with pytest.raises(server.ModelCallError) as caught:
        gen(QUESTION, None)

    assert getattr(caught.value, "tool_lane_refused", False) is True
    assert transport.payloads == []
    assert retrievals == []
    assert seen == [] and lanes._TURN.get() is None
    assert judged == [(LOOKUP_MODEL, False)]
    assert not isinstance(gen, lanes.LookupGenerate)


def test_a_code_mode_ensemble_on_the_lookup_model_looks_nothing_up(checked):
    """Every caller that runs an ensemble's reply asks in code mode, which gets no shared lookup."""
    transport, judged, retrievals = checked
    server.ensemble_answer(QUESTION, tiers=LOOKUP_MODEL, mode="code")

    assert retrievals == []
    assert transport.expansions() == []
    assert all(PASSAGE_TEXT not in p["messages"][-1]["content"] for p in transport.payloads)
    _assert_judged_text_only(judged, len(transport.payloads))
