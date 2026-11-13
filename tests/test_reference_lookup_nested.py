"""Reference lookup state when one chat turn runs inside another.

An MCP chat turn (``server._sonder_impl_serialized``) owns its lookup state:
it starts with none, and on return or on an exception the enclosing turn's
state is put back.  HTTP ``/train`` runs ``server.sonder`` inside an HTTP
turn, so a practice turn must neither leave its record for the HTTP reply
(which would then get a Sources block and a receipt entry it never earned)
nor take or clear the enclosing turn's record.  The MCP route's two early
returns (the web-denial replacement and the guard miss) also settle the
record themselves.

Everything runs through the real lanes with only the Ollama transport faked
(the lookup lanes' ``Transport``); the lookup model is one the policy's
``reference_lookup`` section adds (``LOOKUP_ONLY_MODEL``), so the deny list
does not interfere.
"""
from __future__ import annotations

import json
import os

import pytest

import server
import sonder_runtime.adapters.reference_lookup_lanes as lanes
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.interfaces.http import serve
from tests._reference_lookup_index import LOOKUP_ONLY_MODEL, write_lookup_only_policy
from tests.test_reference_lookup_lanes import (  # noqa: F401  # ``lane`` is a fixture
    ANSWER,
    PASSAGE_TEXT,
    PLAIN_MODEL,
    QUESTION,
    _chat_body,
    _serve_http,
    lane,
)

DENIAL = "I don't have access to the internet, so I can't browse the web for that."
REPLACEMENT = "Here is what the web search found."
SOURCE_URL = "https://example.test/events/4625"


@pytest.fixture
def nested(request, monkeypatch):
    """The lookup lane fixture with ``general`` bound to a lookup-only model."""
    transport = request.getfixturevalue("lane")
    write_lookup_only_policy(os.environ["SONDER_RUNTIME_POLICY"])
    server._refresh_runtime_policy(create=False)
    monkeypatch.setitem(server.TIERS, "general", LOOKUP_ONLY_MODEL)
    monkeypatch.setenv("SONDER_MODEL_ESCALATION", "0")
    monkeypatch.setattr(server, "_get", lambda path: {
        "models": [{"name": LOOKUP_ONLY_MODEL}, {"name": PLAIN_MODEL}]})
    return transport


def _fail_after_lookup(monkeypatch):
    """The code gate's repair call fails after the lookup answer already returned."""
    def gate(*_a, **_k):
        raise ModelCallError("timeout", "repair call timed out")

    monkeypatch.setattr(server, "_gate_answer_code", gate)


def _guard_miss(monkeypatch, transport):
    """The reply denies web access while web tools are on, and the guard finds no replacement."""
    transport.reply = DENIAL
    assert server.web_intents.denies_web_access(DENIAL)
    monkeypatch.setattr(server.web_tools, "enabled", lambda: True)
    monkeypatch.setattr(server, "_route_chat_web", lambda *a, **k: None)
    monkeypatch.setattr(server, "_web_denial_guard", lambda *a, **k: None)


def _train_over_http(monkeypatch, tmp_path, session):
    """POST ``/train 1`` through the real HTTP handler; one practice task asks QUESTION."""
    from tests.test_serve_auth import _http_server, _request

    _serve_http(monkeypatch, tmp_path, False)
    monkeypatch.setitem(server.TIERS, "general", LOOKUP_ONLY_MODEL)  # _serve_http rebinds it
    monkeypatch.setattr(serve.training_tasks, "sample", lambda n: [
        {"name": "t1", "prompt": QUESTION, "check": "assert True"}])
    monkeypatch.setattr(serve, "_http_slash_refusal", lambda *a, **k: "")
    monkeypatch.setattr(serve, "_system_operation_authority_error", lambda *a, **k: "")
    with _http_server(monkeypatch) as port:
        status, _headers, payload = _request(
            port, "POST", "/v1/chat/completions",
            body=_chat_body(session, model="sonder", prompt="/train 1"),
            headers={"Content-Type": "application/json"})
    assert status == 200, payload
    return json.loads(payload)


def _outer_http_turn(monkeypatch):
    """The server half of an HTTP lookup turn: its record waits for ``finish_http``."""
    monkeypatch.setattr(server, "_should_learn", lambda *_a: False)
    text = server._answer_with_history_impl(QUESTION, [], tier="general")
    record = lanes._TURN.get()
    assert record is not None and record.grounded
    return text, record


# --- a turn that ends early leaves nothing behind -----------------------------


def test_an_mcp_turn_that_fails_after_its_lookup_call_leaves_no_state(nested, monkeypatch):
    _fail_after_lookup(monkeypatch)
    server.sonder(QUESTION, tier="general", session="none")

    assert nested.answers(), "the lookup answer call ran"
    assert lanes._TURN.get() is None
    receipt = {}
    assert lanes.finish_http("An unrelated reply.", receipt) == "An unrelated reply."
    assert receipt == {}


def test_http_train_reply_carries_no_sources_from_a_failed_practice_turn(nested, monkeypatch, tmp_path):
    _fail_after_lookup(monkeypatch)
    reply = _train_over_http(monkeypatch, tmp_path, "TR1")

    assert nested.answers(), "the practice turn's lookup call ran"
    content = reply["choices"][0]["message"]["content"]
    assert "practiced 1 tasks" in content
    assert "Sources:" not in content and SOURCE_URL not in content
    assert "reference_lookup" not in reply.get("sonder_receipt", {})


def test_http_train_reply_carries_no_sources_from_a_guard_miss_practice_turn(nested, monkeypatch, tmp_path):
    _guard_miss(monkeypatch, nested)
    reply = _train_over_http(monkeypatch, tmp_path, "TR2")

    assert nested.answers(), "the practice turn's lookup call ran"
    content = reply["choices"][0]["message"]["content"]
    assert "practiced 1 tasks" in content
    assert "Sources:" not in content and SOURCE_URL not in content
    assert "reference_lookup" not in reply.get("sonder_receipt", {})


# --- the MCP route's early returns settle the record themselves -----------------


def _state_at_return(monkeypatch):
    """Record the turn's lookup state each time the MCP route formats its reply."""
    seen = []
    real = server._append_activity

    def spy(text, *args, **kwargs):
        seen.append(lanes._TURN.get())
        return real(text, *args, **kwargs)

    monkeypatch.setattr(server, "_append_activity", spy)
    return seen


def test_the_web_denial_replacement_drops_the_record_and_carries_no_sources(nested, monkeypatch):
    monkeypatch.setattr(server, "_web_denial_guard", lambda *a, **k: REPLACEMENT)
    seen = _state_at_return(monkeypatch)

    out = server.sonder(QUESTION, session="none")  # the guard runs on the default route only

    assert PASSAGE_TEXT in nested.user_turn()  # the replaced reply was a lookup answer
    assert out.startswith(REPLACEMENT) and "Sources:" not in out
    assert seen and all(state is None for state in seen)  # dropped before the reply is formatted


def test_the_guard_miss_reply_keeps_its_own_sources_and_consumes_the_record(nested, monkeypatch):
    """The guard-miss reply is the model's own (passage-grounded) answer, so it is shown with its Sources."""
    _guard_miss(monkeypatch, nested)
    seen = _state_at_return(monkeypatch)

    out = server.sonder(QUESTION, tier="general", session="none")

    assert PASSAGE_TEXT in nested.user_turn()
    assert out.startswith(DENIAL)
    assert "Sources:" in out and SOURCE_URL in out
    assert PASSAGE_TEXT not in out
    assert seen and all(state is None for state in seen)  # taken by with_sources before the reply is formatted


# --- a nested turn neither takes nor clears the enclosing turn's record ----------


def test_a_nested_mcp_turn_keeps_the_enclosing_http_turns_record(nested, monkeypatch):
    text, outer = _outer_http_turn(monkeypatch)

    inner = server.sonder("Say hello.", model_override=PLAIN_MODEL, session="none")

    assert "Sources:" not in inner
    assert lanes._TURN.get() is outer
    receipt = {}
    finished = lanes.finish_http(text, receipt)
    assert finished.count("Sources:") == 1 and SOURCE_URL in finished
    assert receipt["reference_lookup"]["applied"] is True


def test_a_nested_lookup_turn_answers_with_its_own_sources_and_keeps_the_outer_record(nested, monkeypatch):
    _text, outer = _outer_http_turn(monkeypatch)

    inner = server.sonder(QUESTION, tier="general", session="none")

    assert "Sources:" in inner and SOURCE_URL in inner
    assert lanes._TURN.get() is outer


def test_a_nested_turn_that_raises_restores_the_enclosing_record(nested, monkeypatch):
    text, outer = _outer_http_turn(monkeypatch)

    def raising(*_a, **_k):
        raise RuntimeError("guard unavailable")

    monkeypatch.setattr(server, "_gate_answer_code", raising)  # runs after the inner lookup answer
    answers_before = len(nested.answers())
    with pytest.raises(RuntimeError, match="guard unavailable"):
        server.sonder(QUESTION, tier="general", session="none")

    assert len(nested.answers()) == answers_before + 1  # the inner turn did its own lookup call
    assert lanes._TURN.get() is outer
    receipt = {}
    assert lanes.finish_http(text, receipt).count("Sources:") == 1
    assert receipt["reference_lookup"]["applied"] is True
