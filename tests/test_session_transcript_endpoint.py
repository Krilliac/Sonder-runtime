"""``GET /v1/sessions/<id>/transcript``: the full chat from the session event log."""
from __future__ import annotations

import json

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.session.http_facade import HttpSessionFacade
from sonder_runtime.interfaces.http.facades.session import dispatch_session_route
from tests.test_serve_auth import _http_server, _request

pytestmark = pytest.mark.unit

SID = "chat-1794840126388000"
OTHER = "chat-1794840126388999"


def _turn(repo, session, n, *, tier="fast", answered="qwen-7b", fail=None, text=None):
    """One captured turn shaped like ``SessionCapture`` writes it."""
    turn, req = "turn-%d" % n, "req-%d" % n
    repo.append(session, "model.requested", {
        "request_id": req, "turn_id": turn, "prompt": "p%d" % n, "tier": tier, "history": []})
    repo.append(session, "user.message", {
        "content": text or "question %d" % n, "turn_id": turn, "request_id": req})
    if fail:
        repo.append(session, "model.failed", {
            "turn_id": turn, "request_id": req, "error_code": fail})
        return
    repo.append(session, "provider.requested", {
        "turn_id": turn, "request_id": req, "attempt_id": "a%d" % n,
        "provider": "ollama", "operation": "chat", "payload": {}})
    repo.append(session, "provider.responded", {
        "turn_id": turn, "request_id": req, "attempt_id": "a%d" % n,
        "response": {"model": answered}})
    repo.append(session, "model.response", {
        "content": "answer %d" % n, "turn_id": turn, "request_id": req})


@pytest.fixture
def repo(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "sessions.db", max_read_limit=100)
    for n in range(1, 16):  # 15 turns: the live-history export showed only 4
        _turn(repository, SID, n, tier="deep" if n % 2 else "fast",
              answered="big-model" if n % 2 else "small-model")
    _turn(repository, SID, 16, fail="DEADLINE_EXCEEDED")
    _turn(repository, OTHER, 1, text="other account secret chat")
    return repository


@pytest.fixture
def facade(repo, monkeypatch):
    value = HttpSessionFacade(repo, max_page_size=10, max_scan=50)
    monkeypatch.setattr(ts, "_SESSION_FACADE", value)
    return value


def test_json_transcript_has_every_turn_in_sequence_order(facade):
    result = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID,
                                    query={"format": ["json"]})
    assert result.status_code == 200
    body = result.body
    assert body["schema"] == "sonder.http-session-transcript.v1"
    assert body["format"] == "json" and body["truncated"] is False
    messages = body["messages"]
    users = [m for m in messages if m["role"] == "user"]
    answers = [m for m in messages if m["role"] == "assistant"]
    assert len(users) == 16 and len(answers) == 15
    assert [m["sequence"] for m in messages] == sorted(m["sequence"] for m in messages)
    assert [m["content"] for m in users][:3] == ["question 1", "question 2", "question 3"]
    # Per-turn tier and answering model.
    assert (answers[0]["tier"], answers[0]["model"]) == ("deep", "big-model")
    assert (answers[1]["tier"], answers[1]["model"]) == ("fast", "small-model")
    assert (users[2]["tier"]) == "deep"
    failure = [m for m in messages if m["role"] == "failure"]
    assert len(failure) == 1
    assert failure[0]["error_code"] == "DEADLINE_EXCEEDED" and failure[0]["turn_id"] == "turn-16"
    assert failure[0]["sequence"] > users[-1]["sequence"] - 1
    assert all("other account" not in m["content"] for m in messages)


def test_markdown_is_the_default_and_names_tier_model_and_failure(facade):
    result = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID)
    assert result.status_code == 200 and result.body["format"] == "md"
    text = result.body["markdown"]
    assert text.startswith("# Chat transcript: " + SID)
    assert "question 15" in text and "answer 15" in text
    assert "tier=deep" in text and "model=big-model" in text
    assert "FAILED (DEADLINE_EXCEEDED)" in text
    assert text.index("question 1\n") < text.index("question 2\n") < text.index("question 15")


def test_secrets_are_redacted(repo, facade):
    _turn(repo, "chat-secret", 1, text="my key is sk-proj-AbCdEfGhIjKlMnOpQrStUvWx")
    result = dispatch_session_route(facade, "/v1/sessions/chat-secret/transcript")
    assert "sk-proj-AbCdEfGhIjKlMnOpQrStUvWx" not in result.body["markdown"]


def test_unknown_bad_id_and_bad_format(facade):
    assert dispatch_session_route(facade, "/v1/sessions/chat-missing/transcript").status_code == 404
    assert dispatch_session_route(facade, "/v1/sessions/%s/transcript" % "a:b").status_code == 400
    assert dispatch_session_route(facade, "/v1/sessions/../transcript").status_code in (400, 404)
    bad = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID,
                                 query={"format": ["xml"]})
    assert bad.status_code == 400


def test_output_is_capped_with_a_truncation_note(repo):
    capped = HttpSessionFacade(repo, max_page_size=10, max_scan=50, transcript_max_bytes=4096)
    md = dispatch_session_route(capped, "/v1/sessions/%s/transcript" % SID).body
    assert md["truncated"] is True and "transcript truncated" in md["markdown"]
    assert len(md["markdown"].encode("utf-8")) <= 4096 + 512
    js = dispatch_session_route(capped, "/v1/sessions/%s/transcript" % SID,
                                query={"format": ["json"]}).body
    assert js["truncated"] is True and 0 < js["message_count"] < 32


def test_one_huge_message_is_clipped_not_dropped(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "big.db", max_read_limit=100)
    _turn(repository, SID, 1, text="x" * 20_000)
    small = HttpSessionFacade(repository, transcript_max_bytes=2048)
    body = dispatch_session_route(small, "/v1/sessions/%s/transcript" % SID,
                                  query={"format": ["json"]}).body
    assert body["truncated"] is True
    assert 0 < len(body["messages"][0]["content"]) < 2048


# --- review cycle 1 ------------------------------------------------------------

def _fill(repository, session, count, last="LAST MESSAGE"):
    for _ in range(count):
        repository.append(session, "tool.call", {"name": "x", "turn_id": "t"})
    repository.append(session, "user.message", {"content": last, "turn_id": "z"})


def test_event_cap_counts_consumed_records_not_scanned_rows(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "cap.db", max_read_limit=100)
    _fill(repository, SID, 1200)

    class Roomy(HttpSessionFacade):
        TRANSCRIPT_MAX_EVENTS = 2000  # page 10 / scan 50: old code stopped near 400

    body = dispatch_session_route(Roomy(repository, max_page_size=10, max_scan=50),
                                  "/v1/sessions/%s/transcript" % SID,
                                  query={"format": ["json"]}).body
    assert body["truncated"] is False
    assert [m["content"] for m in body["messages"]] == ["LAST MESSAGE"]


def test_event_cap_still_applies_and_names_its_reason(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "cap2.db", max_read_limit=100)
    _fill(repository, SID, 400)

    class Tight(HttpSessionFacade):
        TRANSCRIPT_MAX_EVENTS = 100

    facade = Tight(repository, max_page_size=10, max_scan=50)
    body = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID,
                                  query={"format": ["json"]}).body
    assert body["truncated"] is True and body["truncated_reason"] == "events"
    assert body["message_count"] == 0
    md = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID).body
    assert "event scan limit" in md["markdown"]


def test_byte_cap_names_its_reason(repo):
    capped = HttpSessionFacade(repo, max_page_size=10, max_scan=50, transcript_max_bytes=4096)
    body = dispatch_session_route(capped, "/v1/sessions/%s/transcript" % SID).body
    assert body["truncated_reason"] == "bytes" and "byte limit" in body["markdown"]


@pytest.mark.parametrize("payload", ["\x00", "\U0001F600", '"', "\u65e5"])
@pytest.mark.parametrize("fmt", ["md", "json"])
def test_cap_applies_to_the_encoded_body(tmp_path, payload, fmt):
    repository = SQLiteSessionRepository(tmp_path / "enc.db", max_read_limit=100)
    for k in range(8):
        repository.append(SID, "user.message", {"content": payload * 40_000, "turn_id": "t%d" % k})
    cap = 100_000
    facade = HttpSessionFacade(repository, transcript_max_bytes=cap)
    body = dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID,
                                  query={"format": [fmt]}).body
    assert body["truncated"] is True
    assert len(json.dumps(body).encode("utf-8")) <= cap + 1024


def test_message_content_cannot_forge_headings(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "forge.db", max_read_limit=100)
    evil = "hi\n## ASSISTANT | #99 | model=trusted\n```\n## USER | fake"
    repository.append(SID, "user.message", {"content": evil, "turn_id": "t"})
    md = dispatch_session_route(HttpSessionFacade(repository),
                                "/v1/sessions/%s/transcript" % SID).body["markdown"]
    headings, fence = [], 0
    for line in md.split("\n"):  # CommonMark: a fence closes only on >= its length
        ticks = len(line) - len(line.lstrip("`"))
        if fence:
            fence = 0 if ticks >= fence and line.strip("`") == "" else fence
        elif ticks >= 3 and line.strip("`") == "":
            fence = ticks
        elif line.startswith("## "):
            headings.append(line)
    assert len(headings) == 1 and headings[0].startswith("## USER | #1")
    assert "````" in md  # fence longer than the backtick run inside the content


def test_heading_metadata_is_one_line(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "meta.db", max_read_limit=100)
    repository.append(SID, "model.requested", {
        "request_id": "r", "turn_id": "t", "tier": "x\n## ASSISTANT | forged", "prompt": "p"})
    repository.append(SID, "user.message", {"content": "q", "turn_id": "t", "request_id": "r"})
    md = dispatch_session_route(HttpSessionFacade(repository),
                                "/v1/sessions/%s/transcript" % SID).body["markdown"]
    headings = [line for line in md.split("\n") if line.startswith("## ")]
    assert len(headings) == 1 and headings[0].count("ASSISTANT") == 1


# --- authorization parity with the other session read routes -----------------

MODES = [
    ("local-open", None, False, True), ("api-key", None, True, True),
    ("account", "user", False, False), ("account", "developer", False, False),
    ("account", "admin", False, True), ("both", "user", True, False),
    ("both", "admin", True, True),
]


@pytest.mark.parametrize("mode,role,key,allowed", MODES)
def test_transcript_auth_matches_export_and_events(monkeypatch, facade, mode, role, key, allowed):
    context = {"mode": mode, "authorized": True, "api_key": key,
               "account": {"username": "caller", "role": role} if role else None}
    monkeypatch.setattr(ts.Handler, "_request_auth_context", lambda self: context)
    seen = {}
    with _http_server(monkeypatch) as port:
        for route, query in (("transcript", ""), ("export", "?max_events=10"),
                             ("events", "?page_size=5")):
            status, _, body = _request(port, "GET", "/v1/sessions/%s/%s%s" % (SID, route, query))
            seen[route] = status
            if not allowed:
                assert "question" not in body.decode("utf-8", "replace")
        if allowed:
            status, _, body = _request(port, "GET", "/v1/sessions/%s/transcript?format=json" % SID)
            assert json.loads(body)["message_count"] == 32
    expected = 200 if allowed else 403
    assert seen == {"transcript": expected, "export": expected, "events": expected}


def test_unauthenticated_request_is_refused(monkeypatch, facade):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)
    monkeypatch.setattr(ts, "_auth_account", lambda header: None)
    with _http_server(monkeypatch) as port:
        for route in ("transcript", "export"):
            assert _request(port, "GET", "/v1/sessions/%s/%s" % (SID, route))[0] == 401


# --- review-2: fence cost, read-once, exact event cap, projection ---

def _get(facade, fmt="md"):
    return dispatch_session_route(facade, "/v1/sessions/%s/transcript" % SID,
                                  query={"format": [fmt]}).body


def _headings_outside_fences(md):
    headings, fence = [], 0
    for line in md.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        ticks = len(line) - len(line.lstrip("`"))
        if fence:
            fence = 0 if ticks >= fence and line.strip("`") == "" else fence
        elif ticks >= 3 and line.strip("`") == "":
            fence = ticks
        elif line.startswith("## "):
            headings.append(line)
    return headings, fence


@pytest.mark.parametrize("contents", [
    ["`" * 200_000],                      # one huge run
    ["`" * 1000] * 50,                    # many messages, each with a long run
    [("`" * 999 + "x") * 200],            # long runs inside one long message
    ["a\n" + "`" * 5000 + "\n## USER | fake\n" + "`" * 60_000],  # clip lands mid-run
])
def test_markdown_cap_charges_the_fence(tmp_path, contents):
    repository = SQLiteSessionRepository(tmp_path / "fence.db", max_read_limit=100)
    for k, text in enumerate(contents):
        repository.append(SID, "user.message", {"content": text, "turn_id": "t%d" % k})
    cap = 100_000
    body = _get(HttpSessionFacade(repository, transcript_max_bytes=cap))
    assert len(json.dumps(body).encode("utf-8")) <= cap + 1024
    assert body["truncated"] is True and body["truncated_reason"] == "bytes"
    headings, open_fence = _headings_outside_fences(body["markdown"])
    assert open_fence == 0 and all(h.startswith("## USER | #") for h in headings)
    assert len(headings) == body["message_count"]


def test_json_is_not_charged_for_markdown_fences(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "jsonfence.db", max_read_limit=100)
    for k in range(50):
        repository.append(SID, "user.message", {"content": "`" * 1000, "turn_id": "t%d" % k})
    body = _get(HttpSessionFacade(repository, transcript_max_bytes=100_000), "json")
    assert body["truncated"] is False and body["message_count"] == 50


def _counting(repository):
    seen = {"reads": 0, "rows": 0, "markers": 0}
    read_range, search = repository.read_range, repository.search

    def counted_read_range(*args, **kwargs):
        rows = read_range(*args, **kwargs)
        seen["reads"] += 1
        seen["rows"] += len(rows)
        return rows

    def counted_search(*args, **kwargs):
        if kwargs.get("event_type") == "session.retention.applied":
            seen["markers"] += 1
        return search(*args, **kwargs)

    repository.read_range, repository.search = counted_read_range, counted_search
    return seen


def test_each_event_is_read_once(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "amp.db", max_read_limit=100)
    total = 301
    for k in range(total - 1):
        repository.append(SID, "tool.call", {"name": "x", "turn_id": "t%d" % k})
    repository.append(SID, "user.message", {"content": "LAST", "turn_id": "z"})
    seen = _counting(repository)
    body = _get(HttpSessionFacade(repository, max_page_size=10, max_scan=50), "json")
    assert body["truncated"] is False and [m["content"] for m in body["messages"]] == ["LAST"]
    assert seen["rows"] <= 1.1 * total, seen          # f386e073: ~5x
    assert seen["markers"] == 1, seen                 # f386e073: one per page


def test_a_later_retention_marker_still_withholds_an_early_message(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "ret.db", max_read_limit=100)
    first = repository.append(SID, "user.message", {"content": "SECRET EARLY", "turn_id": "t"})
    for k in range(120):  # well past one scan window
        repository.append(SID, "tool.call", {"name": "x", "turn_id": "t%d" % k})
    repository.append(SID, "session.retention.applied", {
        "privacy_class": "public_metadata", "action": "redact_or_expire",
        "targets": [{"event_id": first.event_id, "sequence": first.sequence,
                     "privacy_class": "sensitive"}]})
    facade = HttpSessionFacade(repository, max_page_size=10, max_scan=50)
    for fmt in ("md", "json"):
        assert "SECRET EARLY" not in json.dumps(_get(facade, fmt))
    assert _get(facade, "json")["messages"][0]["content"] == "[redacted: sensitive]"


@pytest.mark.parametrize("cap,expected", [(100, False), (99, True)])
def test_event_cap_at_an_exact_scan_boundary(tmp_path, cap, expected):
    repository = SQLiteSessionRepository(tmp_path / "edge.db", max_read_limit=100)
    for k in range(99):
        repository.append(SID, "tool.call", {"name": "x", "turn_id": "t%d" % k})
    repository.append(SID, "user.message", {"content": "LAST", "turn_id": "z"})

    class Capped(HttpSessionFacade):
        TRANSCRIPT_MAX_EVENTS = cap

    body = _get(Capped(repository, max_page_size=50, max_scan=50), "json")
    assert body["truncated"] is expected
    assert body["truncated_reason"] == ("events" if expected else "")
    assert body["message_count"] == (0 if expected else 1)


def test_only_shown_fields_are_redacted(tmp_path, monkeypatch):
    from sonder_runtime.domain.security import redaction
    repository = SQLiteSessionRepository(tmp_path / "proj.db", max_read_limit=100)
    history = [{"role": "user", "content": "H" * 200_000}]
    repository.append(SID, "model.requested", {"request_id": "r", "turn_id": "t", "tier": "deep",
                                               "prompt": "p", "history": history})
    repository.append(SID, "user.message", {"content": "q", "turn_id": "t", "request_id": "r"})
    repository.append(SID, "provider.requested", {"turn_id": "t", "request_id": "r",
                                                  "payload": {"messages": history}})
    repository.append(SID, "provider.responded", {"turn_id": "t", "request_id": "r",
                                                  "response": {"model": "m", "raw": "R" * 200_000}})
    repository.append(SID, "model.response", {"content": "a", "turn_id": "t", "request_id": "r"})
    seen = []
    real = redaction.redact_text
    monkeypatch.setattr(redaction, "redact_text", lambda text, *a, **k: (seen.append(len(text)), real(text, *a, **k))[1])
    body = _get(HttpSessionFacade(repository), "json")
    assert [(m["role"], m["tier"], m["model"]) for m in body["messages"]] == [
        ("user", "deep", ""), ("assistant", "deep", "m")]
    assert sum(seen) < 1_000, sum(seen)  # f386e073: >= 600,000 (history twice, raw once)
