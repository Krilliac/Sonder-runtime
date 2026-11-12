"""Reference lookup on the single-model chat lanes (phase B2a).

The MCP chat tool (by tier and by ``model_override``) and the HTTP chat route
(learning and by-name) run end to end here with only the Ollama transport
(``server._post``) faked, against a tiny fixture index.  A lookup model is one
the applies rule matches (the ``astra-cyber-`` prefix); any other model must
see exactly the request it saw before the feature.
"""
from __future__ import annotations

import http.client
import json
import re
import sqlite3
import types

import pytest

import export_training_data
import request_cache
import server
import sonder_runtime.adapters.reference_lookup_lanes as lanes
import sonder_runtime.adapters.reference_lookup_service as service
import sonder_runtime.adapters.reference_lookup_state as state
from sonder_runtime.adapters import embeddings, memory_store, runtime_policy
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.session.provider_attempts import (
    deferred_provider_request_scope,
    dispatch_provider,
)
from sonder_runtime.domain.common.errors import Cancelled
from sonder_runtime.domain.context_formatting import rough_token_count
from sonder_runtime.domain.reference_lookup.selection import passage_char_budget
from sonder_runtime.interfaces.http import serve
from sonder_runtime.platform import reference_library_config as config
from tests._reference_lookup_index import CREATE_META, create_index, rule_cache_cleared


# No applies rule is carried from one test to the next.
_forget_rule_cache = pytest.fixture(autouse=True)(rule_cache_cleared)

LOOKUP_MODEL = "astra-cyber-9b"
PLAIN_MODEL = "qwen2.5:3b"
QUESTION = "What does Windows event 4625 mean after repeated logon attempts?"
PASSAGE_TEXT = "The status 0xC000006A means the user name is correct"
TERMS_REPLY = "failed logon\nlogon failure\n4625"
ANSWER = "Event 4625 is a failed logon [1]."
MARKER_RE = re.compile(r"=== (?:END )?PASSAGE \d+ \[")
ESC = "\x1b"


class Transport:
    """Fake Ollama ``/api/chat``: records every payload, answers by call kind."""

    def __init__(self):
        self.payloads = []
        self.reply = ANSWER
        self.replies = []  # answer replies taken in order before ``reply``
        self.terms = TERMS_REPLY
        self.expand_error = None
        self.answer_error = None
        self.dispatch = False

    @staticmethod
    def is_expansion(payload):
        return payload.get("options", {}).get("num_predict") == lanes.EXPAND_NUM_PREDICT

    def __call__(self, path, payload, *args, **kwargs):
        if path != "/api/chat":
            return {}
        self.payloads.append(json.loads(json.dumps(payload)))
        if self.is_expansion(payload):
            if self.expand_error is not None:
                raise self.expand_error
            content = self.terms
        else:
            if self.answer_error is not None:
                raise self.answer_error
            content = self.replies.pop(0) if self.replies else self.reply
        result = {"message": {"content": content}}
        if self.dispatch:
            return dispatch_provider("ollama", path, payload, lambda: result)
        return result

    def answers(self):
        return [p for p in self.payloads if not self.is_expansion(p)]

    def expansions(self):
        return [p for p in self.payloads if self.is_expansion(p)]

    def user_turn(self):
        return self.answers()[-1]["messages"][-1]["content"]


@pytest.fixture
def lane(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setenv(config.ENV_INDEX, str(create_index(tmp_path / "refs" / "refs.sqlite")))
    monkeypatch.delenv(config.ENV_ENABLED, raising=False)
    monkeypatch.setattr(state, "_INDEX", None)
    monkeypatch.setattr(state, "_LAST_ERROR", "")
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server, "_get",
        lambda path: {"models": [{"name": PLAIN_MODEL}, {"name": LOOKUP_MODEL}]},
    )
    monkeypatch.setattr(embeddings, "embed", lambda text: None)
    transport = Transport()
    monkeypatch.setattr(server, "_post", transport)
    lanes.reset_turn()
    yield transport
    lanes.reset_turn()


def _bind_general(monkeypatch, model):
    monkeypatch.setitem(server.TIERS, "general", model)


def _mcp_by_tier(monkeypatch, model, **kwargs):
    _bind_general(monkeypatch, model)
    return server.sonder(QUESTION, tier="general", **{"session": "none", **kwargs})


def _mcp_by_name(monkeypatch, model, **kwargs):
    return server.sonder(QUESTION, model_override=model, **{"session": "none", **kwargs})


def _http_learning(monkeypatch, model, **kwargs):
    _bind_general(monkeypatch, model)
    monkeypatch.setattr(server, "_should_learn", lambda *_a: True)
    return server._answer_with_history_impl(QUESTION, [], tier="general", **kwargs)


def _http_by_name(monkeypatch, model, **kwargs):
    _bind_general(monkeypatch, model)
    monkeypatch.setattr(server, "_should_learn", lambda *_a: False)
    return server._answer_with_history_impl(QUESTION, [], tier="general", **kwargs)


LANES = {
    "mcp_by_tier": _mcp_by_tier,
    "mcp_by_name": _mcp_by_name,
    "http_learning": _http_learning,
    "http_by_name": _http_by_name,
}
MCP_LANES = ("mcp_by_tier", "mcp_by_name")
HTTP_LANES = ("http_learning", "http_by_name")


def _finish(lane_name, text):
    """What the caller receives: MCP appends Sources itself; HTTP does it in serve.py."""
    if lane_name in HTTP_LANES:
        receipt = {}
        return lanes.finish_http(text, receipt), receipt
    return text, None


def _passthrough_lanes():
    """The lane seams as they behaved before this feature: every call hands back its input."""
    return types.SimpleNamespace(
        lookup_turn=lambda model: False,
        system_for=lambda model, system, caller_system="": system,
        chat_generate=lambda make_generate, question, model, system, temperature,
        num_predict, num_ctx, **options: make_generate(
            model, system, temperature, num_predict, num_ctx, **options),
        mark_learning_skip=lambda conn, iid: False,
        with_sources=lambda text: text,
    )


# --- per lane: the user turn of a lookup model carries the passages ----------


@pytest.mark.parametrize("lane_name", sorted(LANES))
def test_lookup_model_user_turn_carries_the_marker_block_and_passages(lane, monkeypatch, lane_name):
    text, receipt = _finish(lane_name, LANES[lane_name](monkeypatch, LOOKUP_MODEL))

    turn = lane.user_turn()
    assert MARKER_RE.search(turn)
    assert PASSAGE_TEXT in turn
    assert QUESTION in turn
    assert len(lane.expansions()) == 1
    assert len(lane.answers()) == 1
    assert "Sources:" in text and "https://example.test/events/4625" in text
    if receipt is not None:
        assert receipt["reference_lookup"]["applied"] is True
        assert receipt["reference_lookup"]["passages"] >= 1


@pytest.mark.parametrize("lane_name", sorted(LANES))
def test_other_model_request_is_byte_for_byte_what_it_was(lane, monkeypatch, lane_name):
    first, receipt = _finish(lane_name, LANES[lane_name](monkeypatch, PLAIN_MODEL))
    with_feature = list(lane.payloads)
    lane.payloads.clear()

    monkeypatch.setattr(server, "_reference_lookup_lanes", _passthrough_lanes())
    second = LANES[lane_name](monkeypatch, PLAIN_MODEL)

    assert with_feature == lane.payloads
    assert len(with_feature) == 1
    assert not MARKER_RE.search(with_feature[0]["messages"][-1]["content"])
    strip = re.compile(r"\[interaction_id: [0-9a-f]+\]|r\d{6}|\d+ms")
    assert strip.sub("", first) == strip.sub("", second)
    assert "Sources:" not in first
    if receipt is not None:
        assert receipt == {}


# --- storage: only the raw question is stored --------------------------------


def _rows(sql, params=()):
    conn = sqlite3.connect(server._DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def _clean_of_lookup(text):
    return (PASSAGE_TEXT not in text and not MARKER_RE.search(text)
            and "Sources:" not in text and "Reference passages" not in text)


def test_mcp_storage_holds_only_the_raw_question_and_clean_reply(lane, monkeypatch):
    captured = []
    monkeypatch.setattr(
        server, "_capture_durable_session_turn",
        lambda session_id, prompt, history, model, system, tier, response, **kw:
        captured.append({"prompt": prompt, "system": system, "response": response}),
    )
    prompt = "Explain Windows event 4625 after repeated logon attempts. I prefer short answers."
    out = server.sonder(prompt, model_override=LOOKUP_MODEL, session="S1")
    server.sonder("And event 4740?", model_override=LOOKUP_MODEL, session="S1")

    assert "Sources:" in out
    interactions = _rows("SELECT task, response FROM interactions ORDER BY rowid")
    assert interactions[0]["task"] == prompt
    for row in interactions:
        assert _clean_of_lookup(row["task"]) and _clean_of_lookup(row["response"])
    preferences = _rows("SELECT * FROM preferences")
    assert preferences, "the preference phrase should have been captured"
    for row in preferences:
        assert all(_clean_of_lookup(str(value)) for value in row.values())
    assert captured[0]["prompt"] == prompt and captured[0]["system"] == ""
    for item in captured:
        assert _clean_of_lookup(item["prompt"]) and _clean_of_lookup(item["response"])
    # The second turn's history (rebuilt from storage) holds the raw first turn.
    history = lane.answers()[-1]["messages"][:-1]
    assert [m["role"] for m in history] == ["user", "assistant"]
    assert history[0]["content"] == prompt
    assert all(_clean_of_lookup(m["content"]) for m in history)


def test_http_storage_and_returned_text(lane, monkeypatch):
    out = _http_learning(monkeypatch, LOOKUP_MODEL)
    assert "Sources:" not in out  # the server half never adds Sources
    stored = _rows("SELECT task, response FROM interactions")
    assert stored[0]["task"] == QUESTION
    assert _clean_of_lookup(stored[0]["response"])
    returned, receipt = _finish("http_learning", out)
    assert returned.count("Sources:") == 1
    assert receipt["reference_lookup"]["applied"] is True


# --- cleaning ----------------------------------------------------------------


@pytest.mark.parametrize("lane_name", ("mcp_by_name", "http_learning"))
def test_copied_markers_and_control_sequences_are_cleaned(lane, monkeypatch, lane_name):
    lane.reply = (
        "=== PASSAGE 1 [deadbeef] ===\n" + ESC + "[31mEvent 4625" + ESC
        + "[0m is a failed logon [1].\n=== END PASSAGE 1 [deadbeef] ==="
    )
    text, _receipt = _finish(lane_name, LANES[lane_name](monkeypatch, LOOKUP_MODEL))
    stored = _rows("SELECT response FROM interactions")[0]["response"]
    for value in (text, stored):
        assert not MARKER_RE.search(value)
        assert ESC not in value
        assert "Event 4625" in value
    assert "echoed a passage marker line" in text


# --- fail open on every bad index --------------------------------------------


def _text_file(tmp_path):
    path = tmp_path / "notes.sqlite"
    path.write_text("not a database", encoding="utf-8")
    return path


def _plain_table(tmp_path):
    path = tmp_path / "plain.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE passages(title, heading, body, source, url, path, doc)")
    conn.execute(CREATE_META)
    conn.commit()
    conn.close()
    return path


BAD_INDEXES = {
    "missing": lambda tmp_path: tmp_path / "absent" / "refs.sqlite",
    "text_file": _text_file,
    "no_fts5": _plain_table,
    "no_meta": lambda tmp_path: create_index(tmp_path / "nometa.sqlite", meta=False),
}


@pytest.mark.parametrize("lane_name", sorted(LANES))
@pytest.mark.parametrize("bad", sorted(BAD_INDEXES))
def test_bad_index_fails_open_with_a_visible_line(lane, monkeypatch, tmp_path, lane_name, bad):
    monkeypatch.setenv(config.ENV_INDEX, str(BAD_INDEXES[bad](tmp_path)))
    text, receipt = _finish(lane_name, LANES[lane_name](monkeypatch, LOOKUP_MODEL))

    assert ANSWER in text
    assert "Sources: none (reference lookup unavailable: " in text
    assert not MARKER_RE.search(lane.user_turn())
    assert state.last_error()
    if receipt is not None:
        assert receipt["reference_lookup"]["applied"] is False
        assert receipt["reference_lookup"]["reason"]


def test_search_terms_failure_searches_the_question_words(lane, monkeypatch):
    lane.expand_error = OSError("expansion transport down")
    text = _mcp_by_name(monkeypatch, LOOKUP_MODEL)
    assert PASSAGE_TEXT in lane.user_turn()
    assert "Warning: keyword expansion failed" in text
    assert "Sources:\n" in text


# --- no Sonder system prompt on a lookup turn --------------------------------


@pytest.mark.parametrize("lane_name", sorted(LANES))
def test_lookup_turn_sends_no_system_prompt(lane, monkeypatch, lane_name):
    # Sonder's own system prompt (here with the trace instructions on the MCP
    # lanes) is dropped; with no caller system message there is none at all.
    extra = {"trace": True} if lane_name in MCP_LANES else {}
    _finish(lane_name, LANES[lane_name](monkeypatch, LOOKUP_MODEL, **extra))
    for payload in lane.payloads:
        assert payload["messages"][0]["role"] != "system"
    lane.payloads.clear()
    LANES[lane_name](monkeypatch, PLAIN_MODEL, **extra)
    assert lane.payloads[0]["messages"][0]["role"] == "system"
    assert lane.payloads[0]["messages"][0]["content"]


CALLER_SYSTEM = "Answer in one short paragraph for a SOC analyst."


@pytest.mark.parametrize("lane_name", MCP_LANES)
def test_caller_system_message_survives_a_lookup_turn(lane, monkeypatch, lane_name):
    text = LANES[lane_name](monkeypatch, LOOKUP_MODEL, system=CALLER_SYSTEM, trace=True)

    (answer,) = lane.answers()
    # The caller's text, sent as is: no persona, playbook, trace or tool guidance.
    assert answer["messages"][0] == {"role": "system", "content": CALLER_SYSTEM}
    assert MARKER_RE.search(answer["messages"][-1]["content"])
    (expansion,) = lane.expansions()
    assert [m["role"] for m in expansion["messages"]] == ["user"]
    assert "Sources:" in text
    # Control: another model gets Sonder's composed prompt around the caller's text.
    lane.payloads.clear()
    LANES[lane_name](monkeypatch, PLAIN_MODEL, system=CALLER_SYSTEM, trace=True)
    system = lane.payloads[0]["messages"][0]
    assert system["role"] == "system" and CALLER_SYSTEM in system["content"]
    assert system["content"] != CALLER_SYSTEM


def test_http_route_carries_no_caller_system_message_to_keep():
    """The HTTP chat route drops ``system`` role messages on every turn, lookup or
    not, so there is nothing for a lookup turn to keep (B2a behaviour stands)."""
    messages = [{"role": "system", "content": CALLER_SYSTEM},
                {"role": "user", "content": "earlier"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": QUESTION}]
    assert serve._history_from_messages(messages) == [
        {"role": "user", "content": "earlier"}, {"role": "assistant", "content": "reply"}]


def test_search_terms_call_is_small_deterministic_and_bare(lane, monkeypatch):
    _mcp_by_name(monkeypatch, LOOKUP_MODEL)
    (expansion,) = lane.expansions()
    assert expansion["options"]["temperature"] == 0.0
    assert expansion["options"]["num_predict"] == 80
    assert [m["role"] for m in expansion["messages"]] == ["user"]


# --- context budget ----------------------------------------------------------


def test_tiny_context_answers_bare_with_no_room(lane, monkeypatch):
    text = _mcp_by_name(monkeypatch, LOOKUP_MODEL, num_ctx=1200, num_predict=1024)
    assert lane.answers()[-1]["options"]["num_ctx"] == 1200
    assert not MARKER_RE.search(lane.user_turn())
    assert "Sources: none (no room for references)" in text
    assert not lane.expansions()


def test_normal_context_respects_the_budget(lane, monkeypatch):
    seen = []
    real = service.retrieve_for

    def spy(question, expand_fn, k, char_budget):
        bundle = real(question, expand_fn, k, char_budget)
        seen.append((char_budget, bundle))
        return bundle

    monkeypatch.setattr(service, "retrieve_for", spy)
    _mcp_by_name(monkeypatch, LOOKUP_MODEL, num_ctx=2048, num_predict=1024)
    (budget, bundle), = seen
    payload = lane.answers()[-1]
    num_ctx = payload["options"]["num_ctx"]
    framed = payload["messages"][-1]["content"]
    assert bundle.applied
    # The budget was computed on the num_ctx actually sent and the bare prompt.
    assert budget == passage_char_budget(num_ctx, 1024, rough_token_count(QUESTION))
    assert 0 < sum(len(p.text) for p in bundle.passages) <= budget
    assert len(framed) < len(QUESTION) + budget + 2500


# --- request cache and streaming ---------------------------------------------


def _enable_cache(monkeypatch):
    monkeypatch.setenv("SONDER_REQUEST_CACHE", "1")
    monkeypatch.setenv("SONDER_SERVE_TEMPERATURE", "0")
    monkeypatch.setattr(server, "BASE", "http://127.0.0.1:11434")
    monkeypatch.setattr(server, "_cache_model_revision", lambda _model: "sha256:test")
    request_cache.reset_for_tests()


def test_request_cache_never_serves_a_lookup_turn(lane, monkeypatch):
    _enable_cache(monkeypatch)
    statuses = []
    for _ in range(2):
        _http_by_name(monkeypatch, LOOKUP_MODEL, cache_scope="owner",
                      cache_observer=statuses.append)
        lanes.reset_turn()
    assert len(lane.answers()) == 2
    assert statuses == []
    # The same route with another model is cached, so the check above can fail.
    lane.payloads.clear()
    for _ in range(2):
        _http_by_name(monkeypatch, PLAIN_MODEL, cache_scope="owner-2",
                      cache_observer=statuses.append)
    assert len(lane.answers()) == 1
    assert statuses == ["miss", "hit"]


class _ForwardingTransport(Transport):
    """A provider that streams: it forwards its content when it can claim the turn stream."""

    def __call__(self, path, payload, *args, **kwargs):
        with stream_sink.claimed_for_call() as live:
            result = super().__call__(path, payload, *args, **kwargs)
            if live is not None:
                live.emit(result["message"]["content"])
            return result


@pytest.mark.parametrize("lane_name", HTTP_LANES)
def test_streamed_lookup_turn_forwards_nothing_raw(lane, monkeypatch, lane_name):
    transport = _ForwardingTransport()
    monkeypatch.setattr(server, "_post", transport)
    written = []
    live = stream_sink.LiveTurnStream(lambda text: written.append(text) or True)
    with stream_sink.armed(live):
        out = LANES[lane_name](monkeypatch, LOOKUP_MODEL)
    assert written == []
    assert live.claimed and not live.forwarded
    assert "failed logon" not in "".join(written)
    final, _receipt = _finish(lane_name, out)
    assert live.reconcile(final) == (final, False)  # the cleaned final text goes whole
    # Control: another model's turn does stream, so the check above can fail.
    lanes.reset_turn()
    control = stream_sink.LiveTurnStream(lambda text: written.append(text) or True)
    with stream_sink.armed(control):
        LANES[lane_name](monkeypatch, PLAIN_MODEL)
    assert written == [ANSWER]


def test_search_terms_call_is_not_a_provider_attempt(lane, monkeypatch):
    lane.dispatch = True
    admitted, attempts = [], []

    class Capture:
        def begin_provider_attempt(self, pending, *, provider, operation, payload):
            attempts.append(payload)
            return len(attempts)

        def finish_provider_attempt(self, pending, attempt, **_kwargs):
            return None

    with deferred_provider_request_scope(lambda: admitted.append("admit") or (Capture(), object())):
        _mcp_by_name(monkeypatch, LOOKUP_MODEL)
    assert len(lane.payloads) == 2
    # Only the answer call is the turn's provider attempt; the search terms are not.
    assert admitted == ["admit"]
    assert len(attempts) == 1
    assert attempts[0]["options"]["num_predict"] != lanes.EXPAND_NUM_PREDICT


# --- structured lane is excluded ---------------------------------------------


def test_structured_lane_gets_no_lookup(lane, monkeypatch):
    _bind_general(monkeypatch, LOOKUP_MODEL)
    lane.reply = '{"answer": "failed logon"}'
    out = server.structured_answer_with_history(
        QUESTION, [], {"type": "object", "properties": {"answer": {"type": "string"}}},
        tier="general",
    )
    assert "Sources" not in json.dumps(out)
    assert len(lane.payloads) == 1 and not lane.expansions()
    assert not MARKER_RE.search(lane.user_turn())
    assert lanes.with_sources("x") == "x"


# --- learning and export skip ------------------------------------------------


def _skip_rows():
    return _rows(
        "SELECT interaction_id, state, result_reason FROM lesson_distillations "
        "WHERE result_reason=?", (memory_store.LEARNING_SKIP_REFERENCE_LOOKUP,),
    )


def test_lookup_turn_is_marked_and_skipped_by_distillation_and_export(lane, monkeypatch):
    out = _mcp_by_name(monkeypatch, LOOKUP_MODEL, session="M1")
    iid = server.parse_interaction_id(out)
    assert _skip_rows() == [{"interaction_id": iid, "state": "cancelled",
                             "result_reason": "reference_lookup"}]

    other = _mcp_by_name(monkeypatch, PLAIN_MODEL, session="M2")
    other_iid = server.parse_interaction_id(other)
    assert [row["interaction_id"] for row in _skip_rows()] == [iid]

    conn = memory_store.connect(server._DB_PATH)
    try:
        states = {}
        for target in (iid, other_iid):
            recorded = memory_store.record_outcome_and_claim_lesson_distillation(
                conn, target, "accepted", memory_store.memory_rules.reward_score("accepted"),
                source="caller",
            )
            states[target] = recorded["distillation_state"]
            assert recorded["claimed"] is False or target == other_iid
        examples, stats = export_training_data._select_examples(conn)
    finally:
        conn.close()
    assert states[iid] == "cancelled"
    assert states[other_iid] != "cancelled"
    assert stats["rejected_by_reason"].get("reference_lookup") == 1
    # Only the other model's turn is exported (same prompt, so no dedupe hides it).
    assert len(examples) == 1
    assert "duplicate_prompt" not in stats["rejected_by_reason"]


def test_fail_open_turn_is_not_marked(lane, monkeypatch, tmp_path):
    monkeypatch.setenv(config.ENV_INDEX, str(tmp_path / "absent.sqlite"))
    _mcp_by_name(monkeypatch, LOOKUP_MODEL, session="M3")
    assert _skip_rows() == []


def test_lookup_state_never_leaks_into_another_turn(lane, monkeypatch):
    _mcp_by_name(monkeypatch, LOOKUP_MODEL)
    assert lanes.with_sources("plain") == "plain"
    assert lanes.finish_http("plain", {}) == "plain"


# --- HTTP handler: Sources and receipt after capture, streamed whole ---------


def _serve_http(monkeypatch, tmp_path, learning):
    """Set up the real HTTP chat handler over the fake transport; returns the session repository."""
    from types import SimpleNamespace

    from sonder_runtime.adapters.persistence.session_repository import (
        SQLiteSessionRepository,
    )
    from sonder_runtime.application.session.capture import SessionCaptureService
    from sonder_runtime.bootstrap import app as bootstrap_app

    repository = SQLiteSessionRepository(tmp_path / "sessions.sqlite")
    capture = SessionCaptureService(repository)
    monkeypatch.setattr(bootstrap_app, "default_app", lambda: SimpleNamespace(
        session_capture_service=lambda: capture, session_repository=lambda: repository,
    ))
    monkeypatch.setattr(serve, "API_KEY", "")
    monkeypatch.setattr(serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(serve._legacy_runtime(), "prewarm_model", lambda _model: None)
    monkeypatch.setattr(serve, "_server_side_history", lambda _session: [])
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    _bind_general(monkeypatch, LOOKUP_MODEL)
    monkeypatch.setattr(server, "_should_learn", lambda *_a: learning)
    return repository


def _chat_body(session, stream=False, model="general", prompt=QUESTION):
    return json.dumps({"model": model, "session": session, "stream": stream,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")


@pytest.mark.parametrize("learning", (True, False), ids=("http_learning", "http_by_name"))
def test_http_handler_adds_sources_after_capture(lane, monkeypatch, tmp_path, learning):
    from tests.test_serve_auth import _http_server, _request

    repository = _serve_http(monkeypatch, tmp_path, learning)
    recorded = []
    real_record = serve._record_chat
    monkeypatch.setattr(serve, "_record_chat", lambda role, content, **kw: (
        recorded.append((role, content)), real_record(role, content, **kw))[1])

    def send(port, session, stream):
        body = _chat_body(session, stream)
        status, _headers, payload = _request(port, "POST", "/v1/chat/completions", body=body,
                                             headers={"Content-Type": "application/json"})
        assert status == 200, payload
        return payload

    with _http_server(monkeypatch) as port:
        reply = json.loads(send(port, "H1", False))
        frames = [json.loads(line[6:]) for line in send(port, "H2", True).decode("utf-8").splitlines()
                  if line.startswith("data: {")]

    content = reply["choices"][0]["message"]["content"]
    assert ANSWER in content and content.count("Sources:") == 1
    assert reply["sonder_receipt"]["reference_lookup"]["applied"] is True
    deltas = [f["choices"][0]["delta"].get("content") for f in frames
              if f.get("choices") and f["choices"][0]["delta"].get("content")]
    assert len(deltas) == 1 and deltas[0].count("Sources:") == 1  # the cleaned text, whole
    assert "failed logon\nlogon failure" not in "".join(deltas)
    final = next(f for f in frames if f.get("sonder_receipt"))
    assert final["sonder_receipt"]["reference_lookup"]["applied"] is True
    assert [c for role, c in recorded if role == "assistant"] and all(
        _clean_of_lookup(c) for role, c in recorded if role == "assistant")
    for session in ("H1", "H2"):
        events = repository.read_range(session)
        captured = json.dumps([event.payload for event in events], default=str)
        # The capture holds the raw question and the clean answer, and nothing else.
        assert QUESTION in captured and ANSWER in captured, session
        assert "Sources:" not in captured and PASSAGE_TEXT not in captured
        assert not MARKER_RE.search(captured)


def test_http_receipt_says_the_runtime_system_prompt_was_not_sent(lane, monkeypatch):
    for index in (None, "absent"):
        if index:
            monkeypatch.setenv(config.ENV_INDEX, str(server._DB_PATH) + ".absent.sqlite")
        _text, receipt = _finish("http_learning", _http_learning(monkeypatch, LOOKUP_MODEL))
        assert receipt["reference_lookup"][lanes.RECEIPT_SYSTEM_PROMPT_KEY] is False
    _text, receipt = _finish("http_learning", _http_learning(monkeypatch, PLAIN_MODEL))
    assert receipt == {}


# --- the Sources block belongs to the reply that is sent -----------------------


def test_web_denial_replacement_carries_no_sources(lane, monkeypatch):
    replacement = "Here is what the web search found."
    monkeypatch.setattr(server, "_web_denial_guard", lambda *a, **k: replacement)
    text, receipt = _finish("http_learning", _http_learning(monkeypatch, LOOKUP_MODEL))
    assert lane.answers(), "the lookup turn ran before its reply was replaced"
    assert replacement in text and "Sources:" not in text
    assert receipt == {}


class _SampledReplies:
    """Answers for a sampled turn: the dominant answer first, a different one last."""

    DOMINANT = "Event 4625 is a failed logon [1]."
    OUTLIER = "It is something else entirely [9]."


def test_spanda_sampling_shows_the_chosen_samples_sources(lane, monkeypatch, tmp_path):
    from tests.test_serve_auth import _http_server, _request

    _serve_http(monkeypatch, tmp_path, False)
    lane.replies = [_SampledReplies.DOMINANT, _SampledReplies.DOMINANT, _SampledReplies.OUTLIER]
    with _http_server(monkeypatch) as port:
        status, _headers, payload = _request(
            port, "POST", "/v1/chat/completions", body=_chat_body("SP1"),
            headers={"Content-Type": "application/json", "X-Sonder-Spanda": "1"})
    assert status == 200, payload
    reply = json.loads(payload)
    content = reply["choices"][0]["message"]["content"]
    assert len(lane.answers()) == 3 and lane.replies == []
    assert content.startswith(_SampledReplies.DOMINANT) and content.count("Sources:") == 1
    # The last sample cited a passage that was not given; the chosen one did not.
    assert "[9]" not in content
    assert reply["sonder_receipt"]["reference_lookup"]["applied"] is True


def test_keep_alive_turn_after_a_failed_lookup_turn_carries_nothing_from_it(lane, monkeypatch, tmp_path):
    from tests.test_serve_auth import _http_server

    _serve_http(monkeypatch, tmp_path, False)
    monkeypatch.setattr(serve.Handler, "protocol_version", "HTTP/1.1")
    lane.answer_error = ModelCallError("timeout", "the model did not answer")
    plain = "Plain web answer."
    monkeypatch.setattr(server, "route_computer_use", lambda *a, **k: None)
    monkeypatch.setattr(server, "chat_web_response",
                        lambda prompt, **_k: plain if prompt == "hello there" else None)
    results = []
    with _http_server(monkeypatch) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            for body in (_chat_body("K1"), _chat_body("K2", model="sonder", prompt="hello there")):
                conn.request("POST", "/v1/chat/completions", body=body,
                             headers={"Content-Type": "application/json"})
                response = conn.getresponse()
                results.append((response.status, json.loads(response.read()),
                                response.getheader("Connection")))
        finally:
            conn.close()
    (first_status, _first, _), (second_status, second, _) = results
    # The first turn retrieved passages, then its answer call failed before
    # the Sources step; the second (same connection, same thread) is a web reply.
    assert lane.expansions() and first_status != 200
    assert second_status == 200
    content = second["choices"][0]["message"]["content"]
    assert content == plain
    assert "reference_lookup" not in second["sonder_receipt"]


# --- cancellation ---------------------------------------------------------------


@pytest.mark.parametrize("error", [ModelCallError("cancelled", "the caller cancelled the turn"),
                                   Cancelled("the caller cancelled the turn")],
                         ids=("transport", "domain"))
def test_cancelled_search_terms_call_propagates_and_no_answer_is_asked(lane, error):
    asked = []

    def make_generate(model, system, temperature, num_predict, num_ctx, **_options):
        def gen(prompt, history=None, **_kwargs):
            asked.append(num_predict)
            if num_predict == lanes.EXPAND_NUM_PREDICT:
                raise error
            return ANSWER
        return gen

    lanes.system_for(LOOKUP_MODEL, "runtime system prompt")
    gen = lanes.chat_generate(make_generate, QUESTION, LOOKUP_MODEL, "", 0.2, 1024, 8192)
    with pytest.raises(type(error)):
        gen(QUESTION, None)
    assert asked == [lanes.EXPAND_NUM_PREDICT]


def test_cancelled_search_terms_call_ends_the_chat_turn(lane, monkeypatch):
    lane.expand_error = ModelCallError("cancelled", "the caller cancelled the turn")
    with pytest.raises(ModelCallError) as caught:
        _http_learning(monkeypatch, LOOKUP_MODEL, raise_model_errors=True)
    assert caught.value.kind == "cancelled"
    assert len(lane.expansions()) == 1 and lane.answers() == []


# --- the applies rule is read from the policy file only when it changed --------


def test_ordinary_model_turns_do_not_read_the_policy_file_per_rung(lane, monkeypatch):
    built = []
    real = runtime_policy.reference_lookup_rule
    monkeypatch.setattr(runtime_policy, "reference_lookup_rule",
                        lambda policy: built.append(1) or real(policy))
    for _ in range(4):
        _mcp_by_name(monkeypatch, PLAIN_MODEL)
        _http_by_name(monkeypatch, PLAIN_MODEL)
    assert len(lane.answers()) == 8
    assert len(built) <= 1


# --- the passage budget counts the caller's system message ----------------------


def test_budget_counts_the_callers_system_message(lane, monkeypatch):
    seen = []
    real = service.retrieve_for

    def spy(question, expand_fn, k, char_budget):
        seen.append(char_budget)
        return real(question, expand_fn, k, char_budget)

    monkeypatch.setattr(service, "retrieve_for", spy)
    caller_system = "Answer for a SOC analyst. " * 40
    _mcp_by_name(monkeypatch, LOOKUP_MODEL, system=caller_system, num_ctx=2048, num_predict=512)
    (budget,) = seen
    num_ctx = lane.answers()[-1]["options"]["num_ctx"]
    used = rough_token_count(caller_system) + rough_token_count(QUESTION)
    assert budget == passage_char_budget(num_ctx, 512, used)
    assert budget < passage_char_budget(num_ctx, 512, rough_token_count(QUESTION))


# --- a bridged rung is judged by the model its provider serves ------------------


def _bridge_general(monkeypatch, served):
    monkeypatch.setenv("SONDER_GENERAL_PROVIDER", "sonder_inference")
    monkeypatch.setenv("SONDER_INFERENCE_MODEL", served)
    monkeypatch.delenv("SONDER_INFERENCE_TIER_MODELS", raising=False)


def test_bridged_rung_is_judged_by_the_model_its_provider_serves(lane, monkeypatch):
    from sonder_runtime.application.chat import provider_bridge

    _bridge_general(monkeypatch, LOOKUP_MODEL)
    with provider_bridge.bind_rung("sonder_inference", "general"):
        assert lanes.answering_model(PLAIN_MODEL) == LOOKUP_MODEL
        assert lanes.lookup_turn(PLAIN_MODEL) is True
    lanes.reset_turn()
    _bridge_general(monkeypatch, PLAIN_MODEL)
    with provider_bridge.bind_rung("sonder_inference", "general"):
        assert lanes.lookup_turn(LOOKUP_MODEL) is False
    lanes.reset_turn()
    monkeypatch.setenv("SONDER_GENERAL_PROVIDER", "no-such-provider")
    with provider_bridge.bind_rung("sonder_inference", "general"):
        assert lanes.answering_model(LOOKUP_MODEL) == ""
        assert lanes.lookup_turn(LOOKUP_MODEL) is False   # unknown served model: no lookup
    lanes.reset_turn()
    # No rung bound (an Ollama rung): the rung's own model name decides.
    assert lanes.lookup_turn(LOOKUP_MODEL) is True


def test_an_unbound_record_is_not_reused_for_another_binding(lane, monkeypatch):
    from sonder_runtime.application.chat import provider_bridge

    _bridge_general(monkeypatch, LOOKUP_MODEL)
    with provider_bridge.bind_rung("sonder_inference", "general"):
        bridged = lanes._record(PLAIN_MODEL)
        assert bridged.applies is True and bridged.tier == "general"
        assert lanes._record(PLAIN_MODEL) is bridged            # same name, same binding
    # The same model name on an Ollama rung (no binding) is a different decision.
    plain = lanes._record(PLAIN_MODEL)
    assert plain is not bridged
    assert plain.applies is False and plain.tier == ""
    with provider_bridge.bind_rung("sonder_inference", "general"):
        again = lanes._record(PLAIN_MODEL)
    assert again is not plain and again.applies is True
