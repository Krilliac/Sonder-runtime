"""Chat-turn lane (exercise F01, F02, F04, F05, F07, F12): reproducing tests."""
from __future__ import annotations

import contextlib
import inspect
import io
import json
import logging
import time
from types import SimpleNamespace

import pytest

import permission_modes as pm
import server
from sonder_runtime.adapters import model_pace
from sonder_runtime.application.chat import tool_intent
from sonder_runtime.bootstrap import chat_tools_wiring
from sonder_runtime.domain import ollama_policy, thinking_controls, thinking_policy
from sonder_runtime.interfaces.http import live_stream, serve

MINIMUM = server.LOCAL_THINKING_MIN_NUM_PREDICT


# --------------------------------------------------------------------- F01 (a)

def _fake_pool(metadata=None, supports=None):
    snapshot = SimpleNamespace(
        supports_tools=dict(supports or {}), model_metadata=dict(metadata or {}),
    )
    return SimpleNamespace(snapshots=lambda: (snapshot,))


def _turn(monkeypatch, server_ns, *, mode=pm.MANUAL, stream=False):
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    return chat_tools_wiring.build_http_chat_tool_turn(
        server=server_ns,
        permission_policy=SimpleNamespace(current_mode=lambda: mode),
        handler=SimpleNamespace(),
        chunk_fn=lambda *_a, **_k: "",
        model="code", project="", stream=stream, authorized_tools=True,
    )


def _names(turn):
    return [tool.name for tool in turn.manifest.tools]


def _server(pool, **extra):
    return SimpleNamespace(
        _agent_dispatch=lambda *_a, **_k: "", mcp=None, OLLAMA_POOL=pool, **extra,
    )


@pytest.fixture
def show_env(monkeypatch):
    """The real ``server`` with the tier ``code`` bound to ``real-model``; records ``/api/show`` calls."""
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setitem(server.TIERS, "code", "real-model")
    monkeypatch.setattr(server, "_APP_GRAPH", SimpleNamespace(model_gateway=None))
    monkeypatch.setattr(server, "_runtime_installed_model_records",
                        lambda: (("real-model", {"name": "real-model"}),))
    shows = []
    capabilities = {"value": ["completion", "tools"]}

    def post(path, payload, *_a, **_k):
        shows.append((path, payload.get("model")))
        return {"capabilities": list(capabilities["value"])}

    monkeypatch.setattr(server, "_post", post)

    def pool(supports=None):
        monkeypatch.setattr(server, "OLLAMA_POOL", _fake_pool(supports=supports))

    pool()
    return SimpleNamespace(shows=shows, capabilities=capabilities, pool=pool)


def _alias_turn(mode=pm.MANUAL, policy=None):
    return chat_tools_wiring.build_http_chat_tool_turn(
        server=server,
        permission_policy=policy or SimpleNamespace(current_mode=lambda: mode),
        handler=SimpleNamespace(),
        chunk_fn=lambda *_a, **_k: "",
        model="code", project="", stream=False, authorized_tools=True,
    )


def test_alias_model_on_a_cold_cache_gets_the_tool_turn_after_one_show(show_env):
    turn = _alias_turn()
    assert turn is not None
    assert "file_read" in _names(turn)
    assert show_env.shows == [("/api/show", "real-model")]


def test_alias_model_on_a_warm_cache_gets_the_tool_turn_without_a_show(show_env):
    show_env.pool(supports={"real-model": True})
    turn = _alias_turn()
    assert turn is not None
    assert "file_read" in _names(turn)
    assert show_env.shows == []


def test_alias_model_without_the_tools_capability_gets_no_turn(show_env):
    show_env.capabilities["value"] = ["completion"]
    assert _alias_turn() is None
    assert show_env.shows == [("/api/show", "real-model")]


def test_plan_mode_refuses_writes_through_the_real_permission_policy(show_env):
    show_env.pool(supports={"real-model": True})
    policy = SimpleNamespace(
        current_mode=lambda: pm.PLAN,
        decide=lambda tool, **_k: pm.decide(tool, interactive=False, mode=pm.PLAN, record=False),
    )
    turn = _alias_turn(policy=policy)
    assert turn is not None
    assert set(_names(turn)) == chat_tools_wiring.PLAN_MODE_TOOLS
    assert not turn.permission_policy.decide("file_write").allowed


def test_plan_mode_turn_gets_only_the_read_only_tools(monkeypatch):
    ns = _server(_fake_pool(supports={"code": True}))
    turn = _turn(monkeypatch, ns, mode="plan")
    assert turn is not None
    assert set(_names(turn)) == {"file_read", "directory_tree", "text_search", "tool_search"}


# --------------------------------------------------------------------- F01 gate

L2_PROMPT = (
    "merge_intervals in D:/x/trialkit/intervals.py gives wrong answers when intervals touch, "
    "like (1,2) and (2,4). Find the function, read it and explain what is wrong."
)


def test_explicit_model_work_seam_is_gone():
    # An explicit model never starts a work run; there is no seam left to consult.
    assert not hasattr(chat_tools_wiring, "explicit_tier_work_fallback")
    assert not hasattr(chat_tools_wiring, "native_tool_turn_available")


# --------------------------------------------------------------------- F02 (b)

def test_fix_and_search_openers_route_to_file_lanes():
    fix = tool_intent.classify_file_intent("Fix the bug in trialkit/intervals.py")
    assert fix is not None and fix["mode"] == "workbench"
    search = tool_intent.classify_file_intent("Search D:/x/trialkit for TODO")
    assert search is not None and search["mode"] == "inspection"


def test_conceptual_fix_question_and_use_opener_stay_chat():
    assert tool_intent.classify_file_intent("how do I fix a bug in a parser?") is None
    assert tool_intent.classify_file_intent("Use trialkit/intervals.py as the example") is None


# --------------------------------------------------------------------- F04 (c)

@pytest.fixture
def fresh_thinking_state():
    saved = dict(server._THINKING_CAPABILITY_CACHE)
    server._THINKING_CAPABILITY_CACHE.clear()
    model_pace.reset()
    yield
    server._THINKING_CAPABILITY_CACHE.clear()
    server._THINKING_CAPABILITY_CACHE.update(saved)
    model_pace.reset()


def _ask(model="r", num_predict=260, **kwargs):
    return server._chat_request(
        {"model": model, "messages": [{"role": "user", "content": "q"}],
         "options": {"num_predict": num_predict}},
        model=model, **kwargs,
    )


def test_first_call_to_a_metadata_known_thinking_model_gets_headroom(monkeypatch, fresh_thinking_state):
    monkeypatch.setattr(server, "OLLAMA_POOL", _fake_pool(
        metadata={"r": {"capabilities": ["completion", "thinking"]}}))
    seen = []

    def post_model(path, payload, **_kwargs):
        seen.append(payload["options"]["num_predict"])
        return {"message": {"content": "ok"}, "done_reason": "stop"}, 1

    monkeypatch.setattr(server, "_post_model", post_model)
    assert server._known_thinking_model("r") is True
    _ask()
    assert seen == [MINIMUM]


def test_a_model_without_thinking_metadata_is_not_known(monkeypatch, fresh_thinking_state):
    monkeypatch.setattr(server, "OLLAMA_POOL", _fake_pool(
        metadata={"r": {"capabilities": ["completion", "tools"]}}))
    assert server._known_thinking_model("r") is False


# --------------------------------------------------------------------- F04 (d)

def _truncation_after(monkeypatch, replies):
    """Run ``_chat_request`` once per reply inside a turn flag; return the flag."""
    flag = [False]
    token = thinking_controls.REPLY_TRUNCATION.set(flag)
    try:
        queue = list(replies)
        monkeypatch.setattr(server, "_post_model", lambda *a, **k: (queue.pop(0), 1))
        for _ in replies:
            _ask()
    finally:
        thinking_controls.REPLY_TRUNCATION.reset(token)
    return flag[0]


def test_truncated_reply_sets_the_turn_flag(monkeypatch, fresh_thinking_state):
    assert _truncation_after(monkeypatch, [
        {"message": {"content": "half an ans"}, "done_reason": "length"}]) is True


def test_complete_reply_leaves_the_turn_flag_clear(monkeypatch, fresh_thinking_state):
    assert _truncation_after(monkeypatch, [
        {"message": {"content": "whole"}, "done_reason": "stop"}]) is False


def test_truncated_intermediate_call_then_complete_reply_is_not_truncated(monkeypatch, fresh_thinking_state):
    assert _truncation_after(monkeypatch, [
        {"message": {"content": "step one cut"}, "done_reason": "length"},
        {"message": {"content": "the full answer"}, "done_reason": "stop"},
    ]) is False


def test_a_call_outside_a_turn_records_nothing(monkeypatch, fresh_thinking_state):
    monkeypatch.setattr(server, "_post_model", lambda *a, **k: (
        {"message": {"content": "x"}, "done_reason": "length"}, 1))
    assert thinking_controls.REPLY_TRUNCATION.get() is None
    _ask()
    assert thinking_controls.REPLY_TRUNCATION.get() is None


def test_non_stream_finish_reason_is_length_and_receipt_says_truncated():
    obj = serve._chat_completion_object("cut", "code", receipt={"truncated": True})
    assert obj["choices"][0]["finish_reason"] == "length"
    plain = serve._chat_completion_object("whole", "code", receipt={})
    assert plain["choices"][0]["finish_reason"] == "stop"


def test_stream_final_chunk_finish_reason_is_length():
    handler = SimpleNamespace(wfile=io.BytesIO())
    live_stream.write_stream_body(
        handler, serve._chunk, "abc", "code", "cut", 5, {"truncated": True}, None, None)
    frames = [json.loads(line[6:]) for line in handler.wfile.getvalue().decode().splitlines()
              if line.startswith("data: {")]
    assert [f["choices"][0]["finish_reason"] for f in frames if f["choices"][0]["finish_reason"]] == ["length"]


def test_reply_budget_defaults_and_overrides(monkeypatch):
    monkeypatch.delenv("SONDER_CHAT_REPLY_NUM_PREDICT", raising=False)
    monkeypatch.delenv("SONDER_EXPLICIT_REPLY_NUM_PREDICT", raising=False)
    assert server._chat_reply_num_predict(explicit=False) == 1024
    assert server._chat_reply_num_predict(explicit=True) == 4096
    monkeypatch.setenv("SONDER_EXPLICIT_REPLY_NUM_PREDICT", "2048")
    monkeypatch.setenv("SONDER_CHAT_REPLY_NUM_PREDICT", "512")
    assert server._chat_reply_num_predict(explicit=True) == 2048
    assert server._chat_reply_num_predict(explicit=False) == 512
    monkeypatch.setenv("SONDER_CHAT_REPLY_NUM_PREDICT", "junk")
    assert server._chat_reply_num_predict(explicit=False) == 1024


# --------------------------------------------------------------------- F07 (e)

class _Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def pace_clock(monkeypatch):
    clock = _Clock()
    model_pace.reset()
    monkeypatch.setattr(model_pace, "_now", clock)
    yield clock
    model_pace.reset()


def _request(num_predict=260):
    caller = {"model": "m", "messages": [], "options": {"num_predict": num_predict}}
    return server._with_local_thinking_budget(caller), caller


def test_two_exhaustions_make_up_front_send_think_false(pace_clock):
    payload, caller = _request()
    assert "think" not in model_pace.up_front(payload, caller, "m", 300, True)
    model_pace.note_thinking_exhausted("m")
    assert "think" not in model_pace.up_front(payload, caller, "m", 300, True)
    model_pace.note_thinking_exhausted("m")
    sent = model_pace.up_front(payload, caller, "m", 300, True)
    assert sent["think"] is False
    assert sent["options"]["num_predict"] == 260


def test_skip_needs_can_skip_thinking_and_is_per_model(pace_clock):
    payload, caller = _request()
    model_pace.note_thinking_exhausted("m")
    model_pace.note_thinking_exhausted("m")
    assert "think" not in model_pace.up_front(payload, caller, "m", 300, False)
    assert "think" not in model_pace.up_front(payload, caller, "other", 300, True)


def test_an_answered_thinking_request_resets_the_count(pace_clock):
    payload, caller = _request()
    model_pace.note_thinking_exhausted("m")
    model_pace.note_thinking_answered("m")
    model_pace.note_thinking_exhausted("m")
    assert "think" not in model_pace.up_front(payload, caller, "m", 300, True)


def test_skip_expires_after_one_hour(pace_clock):
    payload, caller = _request()
    model_pace.note_thinking_exhausted("m")
    model_pace.note_thinking_exhausted("m")
    pace_clock.now += 3599
    assert model_pace.up_front(payload, caller, "m", 300, True)["think"] is False
    pace_clock.now += 2
    assert "think" not in model_pace.up_front(payload, caller, "m", 300, True)


def test_chat_request_counts_exhaustion_at_the_fallback_and_answers(monkeypatch, fresh_thinking_state, pace_clock):
    exhausted = {"message": {"thinking": "t", "content": ""}, "done_reason": "length"}

    def post_model(path, payload, **_kwargs):
        if payload.get("think") is False:
            return {"message": {"content": "answer"}, "done_reason": "stop"}, 1
        return dict(exhausted), 1

    monkeypatch.setattr(server, "_post_model", post_model)
    for _ in range(2):
        assert _ask()[1] == "answer"
    payload, caller = _request()
    assert model_pace.up_front(payload, caller, "r", 300, True)["think"] is False


# --------------------------------------------------------------------- F12 (f)

def _records(caplog, name, level=logging.INFO):
    return [r for r in caplog.records if r.name.startswith(name) and r.levelno >= level]


def test_thinking_exhaustion_logs_one_record(caplog):
    with caplog.at_level(logging.DEBUG):
        thinking_policy.thinking_exhausted_budget(
            {"done_reason": "length"}, {"thinking": "x", "content": ""})
    assert len(_records(caplog, "sonder_runtime.domain.thinking_policy")) == 1
    assert _records(caplog, "sonder_runtime.domain.thinking_policy", logging.DEBUG)


def test_unterminated_thinking_block_logs_one_record(caplog):
    with caplog.at_level(logging.DEBUG):
        assert thinking_policy.strip_inline_thinking("<think>never closed") == ""
    assert len(_records(caplog, "sonder_runtime.domain.thinking_policy")) == 1


def test_closed_thinking_block_logs_nothing_above_debug(caplog):
    with caplog.at_level(logging.DEBUG):
        assert thinking_policy.strip_inline_thinking("<think>a</think>hi") == "hi"
    assert _records(caplog, "sonder_runtime.domain.thinking_policy") == []


def test_ollama_policy_rejections_log_one_record_each(caplog):
    with caplog.at_level(logging.DEBUG):
        ollama_policy.policy_error("http://user:pw@example.com:11434", allow_remote=True)
    assert len(_records(caplog, "sonder_runtime.domain.ollama_policy")) == 1
    caplog.clear()
    ollama_policy.policy_error("http://203.0.113.9:11434", allow_remote=True)
    with caplog.at_level(logging.DEBUG):
        caplog.clear()
        ollama_policy.policy_error("http://203.0.113.10:11434", allow_remote=True)
    assert len(_records(caplog, "sonder_runtime.domain.ollama_policy")) == 1


def test_exhaustion_round_trip_logs_one_exhaustion_record_and_no_warning(monkeypatch, caplog, fresh_thinking_state, pace_clock):
    exhausted = {"message": {"thinking": "t", "content": ""}, "done_reason": "length"}

    thinking_calls = []

    def post_model(path, payload, **_kwargs):
        if payload.get("think") is False:
            return {"message": {"content": "answer"}, "done_reason": "stop"}, 1
        thinking_calls.append(payload)
        return dict(exhausted), 1

    monkeypatch.setattr(server, "_post_model", post_model)
    server._remember_thinking_model("r")
    with caplog.at_level(logging.INFO):
        _ask()
    exhaustion = [r for r in caplog.records if "thinking budget exhausted" in r.getMessage()]
    assert len(thinking_calls) == 1
    assert len(exhaustion) == len(thinking_calls)
    assert [r for r in caplog.records if r.levelno >= logging.WARNING
            and "thinking" in r.getMessage().lower()] == []


def test_failed_think_false_fallback_logs_one_warning(monkeypatch, caplog, fresh_thinking_state, pace_clock):
    exhausted = {"message": {"thinking": "t", "content": ""}, "done_reason": "length"}

    def post_model(path, payload, **_kwargs):
        if payload.get("think") is False:
            raise server.ModelCallError("request", "boom", attempts=1)
        return dict(exhausted), 1

    monkeypatch.setattr(server, "_post_model", post_model)
    with caplog.at_level(logging.INFO), pytest.raises(server.ModelCallError):
        _ask()
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING
                and "fallback" in r.getMessage().lower()]
    assert len(warnings) == 1


# --------------------------------------------------------------------- F05

@pytest.fixture
def admission_recorder(monkeypatch):
    from sonder_runtime.adapters.inference import ollama_pool

    state = SimpleNamespace(calls=[], active=False, reject=False)

    @contextlib.contextmanager
    def recording(*, caller="agent", timeout_seconds=None):
        if state.reject:
            raise ValueError("caller must be agent, autopilot, fleet, or local")
        state.calls.append((caller, timeout_seconds))
        state.active = True
        try:
            yield
        finally:
            state.active = False

    monkeypatch.setattr(ollama_pool, "local_agent_admission", recording)
    monkeypatch.setattr(live_stream, "_admission_warned", False)
    monkeypatch.setattr(serve._live_stream, "armed_for",
                        lambda *_a, **_k: contextlib.nullcontext())
    return state


def _streamed_turn(state, capture_stream, started_ago=5.0):
    seen = []
    fake_self = SimpleNamespace(_request_started=time.monotonic() - started_ago)
    original = serve._run_prompt
    serve._run_prompt = lambda *_a, **_k: seen.append(state.active) or "reply"
    try:
        out = serve.Handler._run_streamable_prompt(
            fake_self, "p", [], "code", capture_stream=capture_stream)
    finally:
        serve._run_prompt = original
    return out, seen


def test_streamed_turn_runs_inside_an_interactive_stream_scope(admission_recorder):
    out, seen = _streamed_turn(admission_recorder, True)
    assert out == "reply"
    assert seen == [True]
    assert admission_recorder.active is False
    assert len(admission_recorder.calls) == 1
    caller, timeout = admission_recorder.calls[0]
    assert caller == "interactive_stream"
    assert 0 < timeout <= server._bound_request_timeout(None, server.TIMEOUT) - 5


def test_a_turn_that_is_not_streamed_adds_no_scope(admission_recorder):
    out, seen = _streamed_turn(admission_recorder, False)
    assert out == "reply"
    assert seen == [False]
    assert admission_recorder.calls == []


def test_a_pool_that_rejects_the_stream_caller_still_runs_the_turn_and_warns_once(admission_recorder, caplog):
    from sonder_runtime.adapters.inference import ollama_pool

    admission_recorder.reject = True
    scopes = []
    original = serve._run_prompt
    serve._run_prompt = lambda *_a, **_k: scopes.append(ollama_pool._LOCAL_ADMISSION_SCOPE.get()) or "reply"
    try:
        with caplog.at_level(logging.WARNING):
            for _ in range(2):
                fake_self = SimpleNamespace(_request_started=time.monotonic())
                assert serve.Handler._run_streamable_prompt(
                    fake_self, "p", [], "code", capture_stream=True) == "reply"
    finally:
        serve._run_prompt = original
    assert scopes == [None, None]
    warned = [r for r in caplog.records if r.levelno == logging.WARNING
              and "stream admission scope unavailable" in r.getMessage()]
    assert len(warned) == 1


def test_stream_admission_without_a_factory_or_disabled_adds_no_scope(admission_recorder):
    handler = SimpleNamespace()
    with live_stream.stream_admission(handler, 30.0):
        pass
    with live_stream.stream_admission(handler, 30.0, False, admission=chat_tools_wiring.stream_turn_admission):
        pass
    assert admission_recorder.calls == []


def test_the_composition_root_supplies_the_interactive_stream_scope(admission_recorder):
    with live_stream.stream_admission(SimpleNamespace(), 30.0, admission=chat_tools_wiring.stream_turn_admission):
        assert admission_recorder.active is True
    assert admission_recorder.calls == [("interactive_stream", 30.0)]


def test_live_stream_does_not_import_an_adapter():
    assert "adapters" not in inspect.getsource(live_stream)


# --------------------------------------------------------------------- handler level

def _serve_turn_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HTTP_WORK_RUNS_DB", str(tmp_path / "work-runs.sqlite"))
    monkeypatch.setattr(serve, "API_KEY", "")
    monkeypatch.setattr(serve, "AUTH_MODE", "local-open")
    monkeypatch.setattr(serve, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(serve._legacy_runtime(), "prewarm_model", lambda _model: None)
    monkeypatch.setattr(serve._legacy_runtime(), "resolve_discovered_model", lambda _model: None)
    monkeypatch.setattr(serve._legacy_runtime(), "chat_web_response", lambda *_a, **_k: None)
    monkeypatch.setattr(serve, "_handle_intent", lambda *_a, **_k: None)
    monkeypatch.setattr(serve, "_handle_feedback", lambda *_a, **_k: None)
    monkeypatch.setattr(serve, "_server_side_history", lambda _session: [])
    monkeypatch.setattr(serve, "_developer_authorized", lambda _context: True)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", {"routing": {"chat": "fast"}})
    monkeypatch.setattr(server, "_STRICT_DEFAULT", False)
    monkeypatch.setitem(server.TIERS, "fast", "fast-model")


def _post(port, prompt, model):
    from tests.test_serve_auth import _request
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": prompt}]}).encode()
    status, _, raw = _request(port, "POST", "/v1/chat/completions", body=body,
                              headers={"Content-Type": "application/json"})
    assert status == 200, raw
    return json.loads(raw)


def test_explicit_model_with_tools_sends_a_fix_prompt_to_the_native_loop_not_the_work_lane(monkeypatch, tmp_path):
    from tests.test_serve_auth import _http_server

    _serve_turn_env(monkeypatch, tmp_path)
    tools = serve.server._http_chat_tools()
    built = []
    monkeypatch.setattr(tools, "build_http_chat_tool_turn",
                        lambda **kwargs: built.append(kwargs["model"]) or None)
    monkeypatch.setattr(serve, "_handle_work_intent", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("a tool-capable explicit model must use the native tool loop")))
    monkeypatch.setattr(serve._legacy_runtime(), "answer_with_history",
                        lambda prompt, _history, **_k: "model answer")
    with _http_server(monkeypatch) as port:
        body = _post(port, "Fix the bug in trialkit/intervals.py", "code")
    assert "model answer" in body["choices"][0]["message"]["content"]
    assert built == ["code"]


@pytest.mark.parametrize("prompt", [
    "Fix the bug in trialkit/intervals.py", "Review the auth module.",
    "Scan the repo for secrets", "Build the Flutter app.", L2_PROMPT,
    "Read README.md", "Search D:/x/trialkit for TODO", "Implement a parser for the config format.",
])
def test_explicit_model_without_tools_never_starts_a_work_run(monkeypatch, tmp_path, prompt):
    from tests.test_serve_auth import _http_server

    _serve_turn_env(monkeypatch, tmp_path)
    # Hermetic: no live catalog read, and the model has no tool capability.
    monkeypatch.setattr(serve.server, "_runtime_installed_model_records", lambda *_a, **_k: ())
    monkeypatch.setattr(chat_tools_wiring, "_model_supports_tools", lambda *_a, **_k: False)
    monkeypatch.setattr(serve, "_handle_work_intent", lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("explicit model selection must not start work")))
    monkeypatch.setattr(serve._legacy_runtime(), "answer_with_history",
                        lambda prompt, _history, **_k: "model answer")
    with _http_server(monkeypatch) as port:
        body = _post(port, prompt, "code")
    assert "model answer" in body["choices"][0]["message"]["content"]


def _truncation_turn(monkeypatch, tmp_path, flags):
    from tests.test_serve_auth import _http_server

    _serve_turn_env(monkeypatch, tmp_path)

    def answer(prompt, _history, **_k):
        for flag in flags:
            thinking_controls.note_reply_truncation(flag)
        return "model answer"

    monkeypatch.setattr(serve._legacy_runtime(), "answer_with_history", answer)
    with _http_server(monkeypatch) as port:
        return _post(port, "Hello there", "code")


def test_handler_reports_length_only_when_the_returned_reply_was_cut(monkeypatch, tmp_path):
    body = _truncation_turn(monkeypatch, tmp_path, [False, True])
    assert body["choices"][0]["finish_reason"] == "length"
    assert body["sonder_receipt"]["truncated"] is True


def test_handler_reports_stop_when_a_truncated_step_is_followed_by_a_complete_reply(monkeypatch, tmp_path):
    body = _truncation_turn(monkeypatch, tmp_path, [True, False])
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "truncated" not in body["sonder_receipt"]


# --------------------------------------------------------------------- owner decision 8

def test_tool_step_thinking_headroom_is_added_to_the_reply_budget():
    payload = {"options": {"num_predict": 1024}}
    stepped = thinking_controls.with_local_thinking_budget(payload, headroom=1024)
    assert stepped["options"]["num_predict"] == 2048
    assert payload["options"]["num_predict"] == 1024
    # a roomy reply budget still gets its own headroom, and unlimited stays unlimited
    assert thinking_controls.with_local_thinking_budget(
        {"options": {"num_predict": 4096}}, headroom=1024)["options"]["num_predict"] == 5120
    assert thinking_controls.with_local_thinking_budget(
        {"options": {"num_predict": -1}}, headroom=1024)["options"]["num_predict"] == -1


def test_chat_request_gives_a_tool_step_reply_plus_headroom_not_the_flat_floor(monkeypatch, fresh_thinking_state):
    server._remember_thinking_model("r")
    sent = []

    def post_model(path, payload, **_k):
        sent.append(payload["options"]["num_predict"])
        return {"message": {"content": "ok"}, "done_reason": "stop"}, 1

    monkeypatch.setattr(server, "_post_model", post_model)
    server._chat_request(
        {"model": "r", "messages": [{"role": "user", "content": "hi"}], "options": {"num_predict": 1024}},
        model="r", cloud=False, tools=[{"type": "function", "function": {"name": "t"}}],
    )
    assert sent == [1024 + thinking_controls.LOCAL_STEP_THINKING_HEADROOM]


# --------------------------------------------------------------------- owner decision 6 (non-stream approvals)

def test_non_stream_native_turn_refusal_says_to_use_streaming(monkeypatch):
    ns = _server(_fake_pool(supports={"code": True}))
    decision = pm.Decision(action="deny", mode="manual", risk="write", reason="refused", source="unattended", call_id="abc")
    policy = SimpleNamespace(current_mode=lambda: pm.MANUAL, decide=lambda *a, **k: decision)
    monkeypatch.delenv("SONDER_CHAT_TOOL_MODELS", raising=False)
    kwargs = dict(server=ns, permission_policy=policy, handler=SimpleNamespace(), chunk_fn=lambda *_a, **_k: "",
                  model="code", project="", authorized_tools=True)
    plain = chat_tools_wiring.build_http_chat_tool_turn(stream=False, **kwargs)
    streamed = chat_tools_wiring.build_http_chat_tool_turn(stream=True, **kwargs)
    refused = plain.permission_policy.decide("file_write")
    assert "stream" in refused.reason and refused.reason.startswith("refused")
    assert refused.call_id == "abc" and refused.source == "unattended" and not refused.allowed
    assert streamed.permission_policy is policy
