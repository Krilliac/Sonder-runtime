"""Web chat fixes: slash commands under a selected model, code gate and honesty
guidance on selected-model turns, non-chat models in the picker, and session
export reading the event log.  Each test drives the real HTTP handler."""
import http.client
import json
import threading

import pytest

import server as legacy_server
import sonder_runtime.interfaces.http.serve as ts


@pytest.fixture(autouse=True)
def _no_model_discovery(monkeypatch, _configure_http_legacy_boundary):
    monkeypatch.setattr(legacy_server, "discovered_model_records", lambda: [])


_TEST_API_KEY = "k" * 40


@pytest.fixture
def http_port(monkeypatch):
    monkeypatch.setattr(ts, "API_KEY", _TEST_API_KEY)
    monkeypatch.setattr(ts, "AUTH_MODE", "api-key")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    ts._live_telemetry_application(build=True)
    ts.command_catalog.http_slash_tools()
    ts.command_catalog.catalog()
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _post(port, payload, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request(
        "POST", "/v1/chat/completions", body=json.dumps(payload),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + _TEST_API_KEY,
            **(headers or {}),
        },
    )
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, json.loads(body or b"{}")


def _get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    conn.request("GET", path, headers={"Authorization": "Bearer " + _TEST_API_KEY})
    response = conn.getresponse()
    body = response.read()
    conn.close()
    return response.status, json.loads(body or b"{}")


_CODE_REPLY = "Here you go:\n\n```python\nprint('hello')\n```\n"


@pytest.fixture
def model_calls(monkeypatch):
    calls = []

    def answer(prompt, history, **kwargs):
        calls.append(prompt)
        return "model text"

    monkeypatch.setattr(legacy_server, "answer_with_history", answer)
    monkeypatch.setattr(legacy_server, "chat_web_response", lambda *a, **k: None)
    return calls


def _set_mode(monkeypatch, mode):
    import permission_modes
    permission_modes._load()
    monkeypatch.setitem(permission_modes._STATE, "mode", mode)


@pytest.fixture
def run_calls(monkeypatch):
    _set_mode(monkeypatch, "auto")
    calls = []

    def fake_run(code, language="python", **kwargs):
        calls.append((code, language))
        return {"ok": True, "language": language, "cwd": "x", "timeout": 10,
                "returncode": 0, "stdout": "RAN\n", "stderr": ""}

    monkeypatch.setattr(ts.code_runner, "run_code", fake_run)
    return calls


def _with_block(command, model="fast"):
    return {"model": model, "messages": [
        {"role": "user", "content": "write hello"},
        {"role": "assistant", "content": _CODE_REPLY},
        {"role": "user", "content": command},
    ]}


@pytest.mark.parametrize("model", ["fast", "qwen3.5:4b", "sonder"])
def test_run_dispatches_as_command_with_selected_model(
        http_port, model_calls, run_calls, monkeypatch, model):
    monkeypatch.setattr(legacy_server, "resolve_discovered_model_record",
                        lambda name: (name, {"capabilities": ["completion"]}))
    status, body = _post(http_port, _with_block("/run", model))
    assert status == 200, body
    assert "RAN" in body["choices"][0]["message"]["content"]
    assert run_calls == [("print('hello')", "python")]
    assert model_calls == []


def test_run_code_catalogued_tool_dispatches_with_selected_model(
        http_port, model_calls, run_calls):
    status, body = _post(http_port, {"model": "fast", "messages": [
        {"role": "user", "content": "/run_code print(1)"}]})
    assert status == 200, body
    assert run_calls and run_calls[0][0] == "print(1)"
    assert model_calls == []


@pytest.mark.parametrize("text", [
    "/notacommand hi", "/notacommand", "/usr/bin is where it lives",
    "/tmp/x.py", "/usr",
])
def test_unknown_slash_text_still_reaches_selected_model(
        http_port, model_calls, run_calls, text):
    status, body = _post(http_port, {"model": "fast", "messages": [
        {"role": "user", "content": text}]})
    assert status == 200, body
    assert body["choices"][0]["message"]["content"].startswith("model text")
    assert model_calls == [text]
    assert run_calls == []


def test_dangerous_slash_still_forbidden_for_user_with_selected_model(
        http_port, monkeypatch, model_calls, run_calls):
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(
        ts, "_auth_account", lambda header: {"username": "u", "role": "user"})
    status, body = _post(
        http_port, _with_block("/run"), headers={"Authorization": "Bearer t"})
    assert status == 403
    assert body["error"]["type"] == "forbidden_command"
    assert run_calls == [] and model_calls == []


def test_run_is_refused_unattended_in_manual_mode_with_selected_model(
        http_port, model_calls, run_calls, monkeypatch):
    _set_mode(monkeypatch, "manual")
    status, body = _post(http_port, _with_block("/run"))
    assert status == 200, body
    assert "RAN" not in body["choices"][0]["message"]["content"]
    assert run_calls == [] and model_calls == []


def test_run_is_refused_in_plan_mode_with_selected_model(
        http_port, model_calls, run_calls, monkeypatch):
    _set_mode(monkeypatch, "plan")
    status, body = _post(http_port, _with_block("/run"))
    assert status == 200, body
    assert body["choices"][0]["message"]["content"].startswith("refused /run")
    assert run_calls == [] and model_calls == []


_BROKEN_REPLY = "Here:\n\n```python\ndef f():\n    return 1\nraise ValueError('boom')\n```\n"


def _fake_generation(monkeypatch, reply, seen=None):
    """Replace the model call under server.answer_with_history; keep the rest real."""
    monkeypatch.setattr(legacy_server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(legacy_server.web_tools, "enabled", lambda: False)

    def make_generate(model, system, *args, **kwargs):
        def gen(prompt, history=None):
            if seen is not None:
                seen.append({"model": model, "system": system, "prompt": prompt})
            return reply
        return gen

    monkeypatch.setattr(legacy_server, "_make_generate", make_generate)
    monkeypatch.setattr(legacy_server, "_auto_model_context", lambda m: None)
    monkeypatch.setattr(legacy_server, "_should_learn", lambda *a, **k: False)


@pytest.mark.parametrize("tier", ["fast", "cloud-code", None])
def test_selected_model_reply_with_failing_code_gets_not_verified_banner(monkeypatch, tier):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    _fake_generation(monkeypatch, _BROKEN_REPLY)
    out = legacy_server._answer_with_history_impl(
        "write a function", [], tier=tier, capture_session=False)
    assert "NOT VERIFIED" in out, out


def test_code_gate_kill_switch_still_disables_it_for_selected_model(monkeypatch):
    _fake_generation(monkeypatch, _BROKEN_REPLY)
    monkeypatch.setenv("SONDER_CODE_GATE", "0")
    out = legacy_server._answer_with_history_impl(
        "write a function", [], tier="fast", capture_session=False)
    assert "NOT VERIFIED" not in out


@pytest.mark.parametrize("tier", ["fast", "cloud-code", None])
def test_no_verification_guidance_reaches_selected_model_prompt(monkeypatch, tier):
    from sonder_runtime.application.chat import honesty

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    seen = []
    _fake_generation(monkeypatch, "ok", seen)
    legacy_server._answer_with_history_impl(
        "write a python script and validate it", [], tier=tier, capture_session=False)
    assert seen, "the fake model was not called"
    assert honesty._NO_VERIFICATION_GUIDANCE in seen[0]["system"]
    assert seen[0]["system"].count(honesty._NO_VERIFICATION_GUIDANCE) == 1


def test_no_verification_guidance_text_and_idempotence():
    from sonder_runtime.application.chat.honesty import (
        _NO_VERIFICATION_GUIDANCE, no_verification_claims_system)

    for word in ("compiled", "ran", "tested", "verified", "Passed"):
        assert word in _NO_VERIFICATION_GUIDANCE
    assert "/run" not in _NO_VERIFICATION_GUIDANCE
    assert len(_NO_VERIFICATION_GUIDANCE) < 400
    once = no_verification_claims_system("base")
    assert once.startswith("base") and once.endswith(_NO_VERIFICATION_GUIDANCE)
    assert no_verification_claims_system(once) == once
    assert no_verification_claims_system("") == _NO_VERIFICATION_GUIDANCE


# ---- model picker / non-chat models -------------------------------------

_TOY_SHOW = {
    "capabilities": ["completion"],
    "parameters": "num_ctx 256\nnum_predict 40",
    "model_info": {"gpt2.context_length": 256},
}
_GOOD_SHOW = {"capabilities": ["completion", "tools"],
              "model_info": {"llama.context_length": 131072}}
_EMBED_SHOW = {"capabilities": ["embedding"], "model_info": {"bert.context_length": 8192}}
_SHOWS = {"astra-construct0": _TOY_SHOW, "good:7b": _GOOD_SHOW, "embedder": _EMBED_SHOW}


def test_unfit_reason_policy():
    from sonder_runtime.domain.chat_model_fitness import unfit_reason

    assert unfit_reason(_GOOD_SHOW) == ""
    assert "context window 256" in unfit_reason(_TOY_SHOW)
    assert "embedding" in unfit_reason(_EMBED_SHOW)
    assert unfit_reason({"capabilities": ["tools"]}) == "no completion capability"
    assert unfit_reason({"parameters": "num_ctx 1024"}).startswith("context window 1024")
    assert unfit_reason({"model_info": {"x.context_length": 2048}}) == ""
    # nothing declared (older Ollama) or an unusable payload is not evidence
    assert unfit_reason({}) == "" and unfit_reason(None) == ""


def test_fitness_lookup_is_cached_and_fails_open():
    from sonder_runtime.application.chat.model_fitness import ChatModelFitness

    now = [0.0]
    probes = []

    def show(model):
        probes.append(model)
        if model == "down":
            raise RuntimeError("ollama unreachable")
        return _SHOWS[model]

    fitness = ChatModelFitness(show, clock=lambda: now[0])
    assert "context window" in fitness.unfit_reason("astra-construct0")
    assert "context window" in fitness.unfit_reason("ASTRA-construct0")
    assert probes == ["astra-construct0"]
    assert fitness.unfit_reason("down") == ""        # failed probe reads as fit...
    assert fitness.unfit_reason("down") == ""
    assert probes.count("down") == 1                  # ...and is not hammered
    now[0] = 31.0
    fitness.unfit_reason("down")
    assert probes.count("down") == 2                  # retried after the short window
    now[0] = 400.0
    fitness.unfit_reason("astra-construct0")
    assert probes.count("astra-construct0") == 2      # positive entries expire too


@pytest.fixture
def catalog(monkeypatch):
    records = [(name, {"name": name}) for name in _SHOWS]
    monkeypatch.setattr(legacy_server, "discovered_model_records", lambda: records)
    monkeypatch.setattr(legacy_server, "resolve_discovered_model_record",
                        lambda sel: next((r for r in records if r[0] == sel), None))
    monkeypatch.setattr(legacy_server, "_serve_target",
                        lambda sel, strict, **kw: (sel, False, False, "model:") if sel in _SHOWS
                        else (None, False, False, None))
    monkeypatch.setattr(legacy_server, "_CHAT_FITNESS",
                        legacy_server._chat_model_fitness.ChatModelFitness(
                            lambda model: _SHOWS[model]))
    monkeypatch.setattr(legacy_server, "available_tiers", lambda: {})
    return records


def test_model_list_excludes_toy_and_non_completion_models(http_port, catalog):
    status, body = _get(http_port, "/v1/models")
    assert status == 200, body
    ids = [row["id"] for row in body["data"]]
    assert "good:7b" in ids and "sonder" in ids
    assert "astra-construct0" not in ids
    assert "embedder" not in ids


def test_explicit_request_to_toy_model_is_refused_without_a_model_call(
        http_port, catalog, model_calls):
    status, body = _post(http_port, {"model": "astra-construct0", "messages": [
        {"role": "user", "content": "hello"}]})
    assert status == 400, body
    message = body["error"]["message"]
    assert "not chat-capable" in message and "context window 256" in message
    assert model_calls == []


def test_explicit_request_to_fit_model_is_not_refused(http_port, catalog, model_calls):
    status, body = _post(http_port, {"model": "good:7b", "messages": [
        {"role": "user", "content": "hello"}]})
    assert status == 200, body
    assert model_calls == ["hello"]


# ---- session export / listing read the event log ------------------------

@pytest.fixture
def session_log(monkeypatch, tmp_path):
    """A memory store holding 1 learning-route interaction and an event log with 3 turns."""
    from types import SimpleNamespace

    import memory_store
    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
    from sonder_runtime.application.ports.model_gateway import ModelRequest
    from sonder_runtime.application.session.capture import SessionCaptureService

    monkeypatch.setattr(legacy_server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = legacy_server._open_db()
    try:
        memory_store.touch_session(conn, "chat-1", "")
        memory_store.log_interaction(conn, "I0", "first question", "", "first answer",
                                     "code", session_id="chat-1")
    finally:
        conn.close()
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    capture = SessionCaptureService(repo)
    for index, tier in enumerate(["code", "fast", "kimi-k3:cloud"]):
        capture.capture_turn(
            "chat-1", "turn-%d" % index, ModelRequest(prompt="q%d" % index, tier=tier),
            request_id="req-%d" % index, user_message="q%d" % index,
            model_response="a%d" % index)
    pending = capture.begin_request(
        "chat-1", "turn-3", ModelRequest(prompt="q3", tier="astra-construct0"),
        request_id="req-3", user_message="q3")
    capture.fail_request(pending, error_code="DEPENDENCY_UNAVAILABLE")
    monkeypatch.setattr(legacy_server, "_application",
                        lambda: SimpleNamespace(session_repository=lambda: repo))
    return repo


def test_session_export_includes_selected_model_and_cloud_turns(session_log):
    out = legacy_server.session_export("chat-1")
    for index in range(4):
        assert "USER: q%d" % index in out
    for index in range(3):
        assert "ASSISTANT: a%d" % index in out
    assert "[no response: DEPENDENCY_UNAVAILABLE]" in out
    assert "USER: first question" in out      # the pre-log interaction row is kept
    assert out.count("USER:") == 5


def test_session_export_limit_keeps_the_latest_turns(session_log):
    out = legacy_server.session_export("chat-1", limit=2)
    assert "USER: q3" in out and "USER: q2" in out and "USER: q1" not in out


def test_session_export_without_event_log_keeps_interaction_rows(monkeypatch, session_log):
    from types import SimpleNamespace

    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository

    empty = SQLiteSessionRepository(session_log._db_path.parent / "empty.db")
    monkeypatch.setattr(legacy_server, "_application",
                        lambda: SimpleNamespace(session_repository=lambda: empty))
    out = legacy_server.session_export("chat-1")
    assert out.count("USER:") == 1 and "USER: first question" in out


def test_session_export_atif_counts_event_log_turns(session_log):
    document = json.loads(legacy_server.session_export("chat-1", format="atif"))
    assert sum(1 for step in document["steps"] if step["source"] == "user") == 5
    assert document["extra"]["sonder_source"] == "session_events"


def test_sessions_listing_counts_every_user_turn(session_log):
    out = legacy_server.sonder_sessions()
    assert "chat-1  [5 turns]" in out


def test_event_turns_group_by_turn_and_tolerate_unknown_events():
    from types import SimpleNamespace

    from sonder_runtime.application.session.event_turns import turns_from_events

    def ev(kind, **payload):
        return SimpleNamespace(event_type=kind, payload=payload, event_id="e", occurred_at_utc="t")

    turns = turns_from_events([
        ev("model.requested", turn_id="a", tier="fast"),
        ev("user.message", turn_id="a", content="hi"),
        ev("provider.requested", turn_id="a"),
        ev("model.response", turn_id="a", content="yo"),
        ev("model.response", turn_id="orphan", content="ignored"),
    ])
    assert [(t["task"], t["response"], t["tier"]) for t in turns] == [("hi", "yo", "fast")]


def _user_steps(document):
    return [step["message"] for step in document["steps"] if step["source"] == "user"]


def _retain(repo, session_id, text, label="private"):
    """Append a retention marker withholding the user.message holding ``text``."""
    event = next(e for e in repo.read_complete(session_id)
                 if e.event_type == "user.message" and e.payload.get("content") == text)
    repo.append(session_id, "session.retention.applied", {
        "privacy_class": "public_metadata", "action": "redact_or_expire",
        "targets": [{"event_id": event.event_id, "sequence": event.sequence,
                     "privacy_class": label}]})


def _capture_turn(repo, session_id, index, user, reply, tier="fast"):
    from sonder_runtime.application.ports.model_gateway import ModelRequest
    from sonder_runtime.application.session.capture import SessionCaptureService
    SessionCaptureService(repo).capture_turn(
        session_id, "pturn-%d" % index, ModelRequest(prompt=user, tier=tier),
        request_id="preq-%d" % index, user_message=user, model_response=reply)


def test_session_export_withholds_retention_marked_turns(session_log):
    _capture_turn(session_log, "chat-1", 9, "PRIVATE-CANARY-91", "PRIVATE-REPLY-92")
    _retain(session_log, "chat-1", "PRIVATE-CANARY-91")
    out = legacy_server.session_export("chat-1")
    assert "PRIVATE-CANARY-91" not in out
    assert "[redacted: private]" in out
    assert "USER: q0" in out                    # unmarked turns are untouched


def test_session_export_atif_withholds_retention_marked_turns(session_log):
    _capture_turn(session_log, "chat-1", 9, "PRIVATE-CANARY-91", "PRIVATE-REPLY-92")
    _retain(session_log, "chat-1", "PRIVATE-CANARY-91")
    raw = legacy_server.session_export("chat-1", format="atif")
    assert "PRIVATE-CANARY-91" not in raw
    assert "[redacted: private]" in raw


def test_session_export_fails_closed_on_a_malformed_retention_marker(session_log):
    session_log.append("chat-1", "session.retention.applied", {"targets": "nope"})
    out = legacy_server.session_export("chat-1")
    assert "USER: first question" in out        # falls back to the interaction rows
    assert "USER: q0" not in out


def test_session_export_malformed_retention_never_leaks_retained_learning_turn(
        monkeypatch, tmp_path):
    from types import SimpleNamespace

    import memory_store
    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
    from sonder_runtime.domain.common.errors import InvalidInput

    monkeypatch.setattr(legacy_server, "_DB_PATH", str(tmp_path / "mem.db"))
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    monkeypatch.setattr(legacy_server, "_application",
                        lambda: SimpleNamespace(session_repository=lambda: repo))

    # Session 1: a pre-log row plus a learning-route turn logged in BOTH SQLite
    # interactions and session_events, withheld by retention, followed by a
    # malformed retention marker.
    conn = legacy_server._open_db()
    try:
        memory_store.touch_session(conn, "s-mix", "")
        memory_store.log_interaction(conn, "PRE0", "PRELOG-SAFE-1", "",
                                     "prelog-answer", "code", session_id="s-mix")
        memory_store.log_interaction(conn, "SEC1", "SECRET-USER-999", "",
                                     "SECRET-REPLY-999", "code", session_id="s-mix")
        memory_store.touch_session(conn, "s-only", "")
        memory_store.log_interaction(conn, "SEC2", "SECRET-ONLY-888", "",
                                     "SECRET-ONLY-REPLY-888", "code", session_id="s-only")
    finally:
        conn.close()

    for sid, prompt, reply in (
        ("s-mix", "SECRET-USER-999", "SECRET-REPLY-999"),
        ("s-only", "SECRET-ONLY-888", "SECRET-ONLY-REPLY-888"),
    ):
        _capture_turn(repo, sid, 1, prompt, reply)
        events = list(repo.read_complete(sid))
        targets = [
            {"event_id": e.event_id, "sequence": e.sequence, "privacy_class": "private"}
            for e in events if e.event_type in {"user.message", "model.response"}
        ]
        repo.append(sid, "session.retention.applied", {
            "privacy_class": "public_metadata", "action": "redact_or_expire",
            "targets": targets,
        })
        repo.append(sid, "session.retention.applied", {"targets": "corrupted"})

    # Session with a pre-log row keeps only the pre-log row in both formats.
    mix_text = legacy_server.session_export("s-mix")
    assert "PRELOG-SAFE-1" in mix_text
    assert "SECRET-USER-999" not in mix_text
    assert "SECRET-REPLY-999" not in mix_text
    mix_atif = legacy_server.session_export("s-mix", format="atif")
    assert "PRELOG-SAFE-1" in mix_atif
    assert "SECRET-USER-999" not in mix_atif
    assert "SECRET-REPLY-999" not in mix_atif

    # Session with no pre-log rows leaks nothing in text and fails closed in ATIF.
    only_text = legacy_server.session_export("s-only")
    assert "SECRET-ONLY-888" not in only_text
    assert "SECRET-ONLY-REPLY-888" not in only_text
    with pytest.raises(InvalidInput):
        legacy_server.session_export("s-only", format="atif")


def test_session_export_merges_pre_log_rows_with_event_log_turns(monkeypatch, tmp_path):
    from types import SimpleNamespace

    import memory_store
    from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository

    monkeypatch.setattr(legacy_server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = legacy_server._open_db()
    try:
        memory_store.touch_session(conn, "mix-1", "")
        for index in range(3):      # OLDQ rows predate the log
            memory_store.log_interaction(conn, "O%d" % index, "OLDQ%d" % index, "",
                                         "olda%d" % index, "code", session_id="mix-1")
        # NEWQ0 was also a learning-route turn: logged in both places, listed once.
        memory_store.log_interaction(conn, "N0", "NEWQ0", "", "newa0", "code", session_id="mix-1")
    finally:
        conn.close()
    repo = SQLiteSessionRepository(tmp_path / "sessions.db")
    for index in range(3):
        _capture_turn(repo, "mix-1", index, "NEWQ%d" % index, "newa%d" % index)
    monkeypatch.setattr(legacy_server, "_application",
                        lambda: SimpleNamespace(session_repository=lambda: repo))
    out = legacy_server.session_export("mix-1")
    users = [line for line in out.splitlines() if line.startswith("USER:")]
    assert users == ["USER: OLDQ0", "USER: OLDQ1", "USER: OLDQ2",
                     "USER: NEWQ0", "USER: NEWQ1", "USER: NEWQ2"]
    assert _user_steps(json.loads(legacy_server.session_export("mix-1", format="atif"))) == [
        "OLDQ0", "OLDQ1", "OLDQ2", "NEWQ0", "NEWQ1", "NEWQ2"]
    assert "mix-1  [6 turns]" in legacy_server.sonder_sessions()


# ---- _slash_messages: server-side history for a thin client ---------------

_STORED = [{"role": "user", "content": "write hello"},
           {"role": "assistant", "content": _CODE_REPLY}]


@pytest.fixture
def stored_history(monkeypatch):
    """Server-side history; with ``owners`` set, only those storage ids have any."""
    seen, owners = [], set()

    def fake(storage_session, limit=None):
        seen.append(storage_session)
        return list(_STORED) if (storage_session in owners or not owners) else []

    monkeypatch.setattr(ts, "_server_side_history", fake)
    return seen, owners


def test_thin_client_run_uses_the_stored_code_block(
        http_port, model_calls, run_calls, stored_history):
    status, body = _post(http_port, {"model": "fast", "session": "s1", "messages": [
        {"role": "user", "content": "/run"}]})
    assert status == 200, body
    assert run_calls == [("print('hello')", "python")]
    assert model_calls == []


def test_thin_client_run_with_history_client_does_not_inject(
        http_port, model_calls, run_calls, stored_history):
    status, body = _post(http_port, {"model": "fast", "session": "s1", "history": "client",
                                     "messages": [{"role": "user", "content": "/run"}]})
    assert status == 200, body
    assert run_calls == []
    assert stored_history[0] == []


def test_client_earlier_user_turns_are_not_duplicated_or_merged(
        http_port, model_calls, run_calls, stored_history):
    status, body = _post(http_port, {"model": "fast", "session": "s1", "messages": [
        {"role": "user", "content": "hi"}, {"role": "user", "content": "/run"}]})
    assert status == 200, body
    assert run_calls == []                       # the client owns its transcript


def test_thin_client_run_never_reads_another_principals_session(
        http_port, model_calls, run_calls, stored_history, monkeypatch):
    seen, owners = stored_history
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "_auth_account", lambda header: {
        "username": header.split()[-1], "role": "admin"})
    payload = {"model": "fast", "session": "shared-name",
               "messages": [{"role": "user", "content": "/run"}]}
    status, body = _post(http_port, payload, headers={"Authorization": "Bearer alice"})
    assert status == 200, body
    assert run_calls == [("print('hello')", "python")]
    alice = seen[-1]
    assert alice != "shared-name" and alice.startswith("http-session-")
    owners.add(alice)                             # only alice's storage id has history
    run_calls.clear()
    status, body = _post(http_port, payload, headers={"Authorization": "Bearer bob"})
    assert status == 200, body
    assert seen[-1] != alice and run_calls == []
    status, body = _post(http_port, payload, headers={"Authorization": "Bearer alice"})
    assert status == 200, body
    assert run_calls == [("print('hello')", "python")]


# ---- model rows: the tier loop honours fitness -----------------------------

def test_model_list_excludes_unfit_tiers(http_port, catalog, monkeypatch):
    monkeypatch.setattr(legacy_server, "available_tiers",
                        lambda: {"toytier": "astra-construct0", "goodtier": "good:7b"})
    status, body = _get(http_port, "/v1/models")
    assert status == 200, body
    ids = [row["id"] for row in body["data"]]
    assert "toytier" not in ids and "goodtier" in ids
