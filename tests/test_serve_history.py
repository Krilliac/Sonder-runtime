import trilobite_serve as ts
from concurrent.futures import ThreadPoolExecutor
import threading


def test_history_from_messages_excludes_last_user_turn():
    messages = [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "follow up"},  # current prompt -> excluded
    ]
    hist = ts._history_from_messages(messages)
    assert hist == [
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
    ]


def test_history_from_messages_drops_system_and_empty():
    messages = [
        {"role": "system", "content": "you are trilobite"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": ""},   # empty -> dropped
        {"role": "user", "content": "current"},
    ]
    hist = ts._history_from_messages(messages)
    assert hist == [{"role": "user", "content": "q1"}]


def test_history_from_messages_single_turn_is_empty():
    hist = ts._history_from_messages([{"role": "user", "content": "only turn"}])
    assert hist == []


def test_history_from_messages_handles_empty():
    assert ts._history_from_messages([]) == []
    assert ts._history_from_messages(None) == []


def test_model_to_tier_defaults_to_local_student():
    for m in ("", None, "trilobite", "gpt-4o-mini"):
        assert ts._model_to_tier(m) is None


def test_model_to_tier_selects_known_tier():
    assert ts._model_to_tier("cloud-code") == "cloud-code"
    assert ts._model_to_tier("general") == "general"


def test_model_to_tier_unknown_falls_back_to_default():
    assert ts._model_to_tier("some-random-model") is None


def test_do_run_returns_structured_error_for_input_program():
    ts.LAST_RESPONSE = "```python\ninput('move: ')\n```"
    out = ts._do_run()

    assert "status: failed" in out
    assert "stderr:" in out
    assert "EOFError" in out
    assert "[exited with error]" in out


def test_run_accepts_optional_timeout():
    ts.LAST_RESPONSE = "```python\nprint('slow smoke')\n```"
    ts.LAST_RUN_SOURCE = None
    out = ts._handle_slash("/run 12")

    assert "timeout: 12s" in out
    assert "slow smoke" in out


def test_run_uses_answer_source_not_trace(monkeypatch):
    seen = {}
    ts.LAST_RESPONSE = (
        "```python\nprint('answer')\n```\n"
        "=== TRACE (how trilobite decided) ===\n"
        "```python\nprint('trace')\n```"
    )
    ts.LAST_RUN_SOURCE = "```python\nprint('answer')\n```"

    def fake_run(code, language="python", timeout=8):
        seen["code"] = code
        return {"ok": True, "stdout": "answer", "stderr": "", "timeout": timeout, "returncode": 0}

    monkeypatch.setattr(ts.code_runner, "run_code", fake_run)
    monkeypatch.setattr(ts.code_runner, "format_result", lambda result: result["stdout"])

    out = ts._handle_slash("/run")

    assert out.endswith("[ran OK]")
    assert seen["code"] == "print('answer')"


def test_run_falls_back_to_prior_assistant_message(monkeypatch):
    seen = {}
    ts.LAST_RESPONSE = "dumped chat/debug log to dump.txt"
    ts.LAST_RUN_SOURCE = "dumped chat/debug log to dump.txt"
    messages = [
        {"role": "user", "content": "make cpp"},
        {"role": "assistant", "content": "```cpp\nint main(){return 0;}\n```"},
        {"role": "user", "content": "/dump"},
        {"role": "assistant", "content": "dumped chat/debug log to dump.txt"},
        {"role": "user", "content": "/run"},
    ]

    def fake_run(code, language="python", timeout=8):
        seen["code"] = code
        seen["language"] = language
        return {"ok": True, "stdout": "compiled", "stderr": "", "timeout": timeout, "returncode": 0}

    monkeypatch.setattr(ts.code_runner, "run_code", fake_run)
    monkeypatch.setattr(ts.code_runner, "format_result", lambda result: result["stdout"])

    out = ts._handle_slash("/run", messages=messages)

    assert out.endswith("[ran OK]")
    assert seen == {"code": "int main(){return 0;}", "language": "cpp"}


def test_runwindow_uses_prior_runnable_block(monkeypatch):
    seen = {}
    ts.LAST_RESPONSE = "```cpp\nint main(){return 0;}\n```"
    ts.LAST_RUN_SOURCE = None

    def fake_run_window(code, language="python", timeout=8):
        seen["code"] = code
        seen["language"] = language
        seen["timeout"] = timeout
        return {
            "ok": True,
            "stdout": "launched",
            "stderr": "",
            "timeout": timeout,
            "returncode": None,
            "language": language,
            "cwd": "C:/repo",
            "error": "",
            "detached": True,
            "run_dir": "C:/tmp/trilobite-window",
        }

    monkeypatch.setattr(ts.code_runner, "run_code_window", fake_run_window)

    out = ts._handle_slash("/runwindow 7")

    assert out.endswith("[launched]")
    assert "run dir: C:/tmp/trilobite-window" in out
    assert seen == {"code": "int main(){return 0;}", "language": "cpp", "timeout": 7}


def test_run_rejects_invalid_timeout():
    out = ts._handle_slash("/run python pong.py")

    assert out.startswith("usage: /run [seconds]")
    assert "previous fenced code block" in out


def test_passive_slash_records_copied(monkeypatch):
    seen = []
    monkeypatch.setattr(ts.server, "record_outcome", lambda iid, signal: seen.append((iid, signal)) or "ok")
    ts.LAST_IID = "abc123"

    out = ts._handle_slash("/copied")

    assert out == "ok"
    assert seen == [("abc123", "copied")]
    assert ts.LAST_IID is None


def test_improve_slash_returns_report(monkeypatch):
    monkeypatch.setattr(ts.server, "system_improvement_report", lambda: "improve me")

    assert ts._handle_slash("/improve") == "improve me"
    assert ts._handle_slash("/improvements") == "improve me"


def test_master_slash_routes_modes(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ts.server,
        "master_orchestrate",
        lambda **kwargs: calls.append(kwargs) or "mastered",
    )

    assert ts._handle_slash("/master delegate build it") == "mastered"
    assert ts._handle_slash("/master delagte build it") == "mastered"
    assert ts._handle_slash("/master fleet build it") == "mastered"
    assert calls == [
        {"task": "build it", "mode": "delegate"},
        {"task": "build it", "mode": "delegate"},
        {"task": "build it", "mode": "fleet"},
    ]


def test_capacity_and_cancel_slashes_route_to_control_command(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ts.server,
        "control_command",
        lambda command, **kwargs: calls.append((command, kwargs)) or "controlled",
    )

    assert ts._handle_slash("/capacity 12", project="demo") == "controlled"
    assert ts._handle_slash("/agentcancel all", project="demo") == "controlled"
    assert ts._handle_slash("/agentretry master-old", project="demo") == "controlled"
    assert calls == [
        ("/capacity 12", {"project": "demo"}),
        ("/agentcancel all", {"project": "demo"}),
        ("/agentretry master-old", {"project": "demo"}),
    ]


def test_agents_slash_returns_master_status(monkeypatch):
    monkeypatch.setattr(ts.server, "master_status", lambda: "agents live")

    assert ts._handle_slash("/agents") == "agents live"


def test_weather_slash_routes_to_live_tool(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ts.server,
        "weather_lookup",
        lambda location: calls.append(location) or "weather live",
    )

    assert ts._handle_slash("/weather Chicago, IL") == "weather live"
    assert ts._handle_slash("/weather") == "usage: /weather <city/state or ZIP>"
    assert calls == ["Chicago, IL"]


def test_workbench_slash_routes_project_and_task(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ts.server,
        "workbench_agent",
        lambda **kwargs: calls.append(kwargs) or "work complete",
    )

    assert ts._handle_slash("/work inspect and test", project="demo") == "work complete"
    assert calls == [{
        "prompt": "inspect and test",
        "tier": "code",
        "max_steps": 12,
        "project": "demo",
    }]
    assert ts._handle_slash("/work") == "usage: /work <task>"


def test_natural_execution_intent_requires_developer_authorization(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ts.server,
        "route_work_request",
        lambda prompt, project="": calls.append((prompt, project)) or "grounded work",
    )

    assert ts._handle_work_intent("edit the app files", authorized=False) is None
    assert ts._handle_work_intent(
        "edit the app files", project="demo", authorized=True
    ) == "grounded work"
    assert calls == [("edit the app files", "demo")]


def test_contextsize_slash_shows_or_sets(monkeypatch):
    monkeypatch.setattr(ts.server, "context_policy_status", lambda: "policy")
    monkeypatch.setattr(ts.server, "set_context_size", lambda size: "set " + size)

    assert ts._handle_slash("/contextsize") == "policy"
    assert ts._handle_slash("/ctxsize 1m") == "set 1m"


def test_claude_like_slash_controls_route(monkeypatch):
    monkeypatch.setattr(ts.server, "context_compaction_plan", lambda: "compact")
    monkeypatch.setattr(ts.server, "command_registry_list", lambda f: "commands " + f)
    monkeypatch.setattr(ts.server, "permission_policy", lambda tool: "perms " + tool)
    monkeypatch.setattr(ts.server, "task_list", lambda: "tasks")
    monkeypatch.setattr(ts.server, "task_create", lambda title: "create " + title)
    monkeypatch.setattr(ts.server, "task_update", lambda task_id, status: "update %s %s" % (task_id, status))
    monkeypatch.setattr(ts.server, "task_show", lambda task_id: "show " + task_id)

    assert ts._handle_slash("/compact") == "compact"
    assert ts._handle_slash("/commands risk") == "commands risk"
    assert ts._handle_slash("/permissions file_delete") == "perms file_delete"
    assert ts._handle_slash("/todo") == "tasks"
    assert ts._handle_slash("/todo add ship it") == "create ship it"
    assert ts._handle_slash("/todo start abc") == "update abc in_progress"
    assert ts._handle_slash("/todo done abc") == "update abc done"
    assert ts._handle_slash("/todo block abc") == "update abc blocked"
    assert ts._handle_slash("/todo show abc") == "show abc"


def test_dump_slash_writes_debug_file(monkeypatch, tmp_path):
    monkeypatch.setattr(ts.server.trilobite_paths, "default_home", lambda: tmp_path)
    monkeypatch.setattr(ts.server, "context_health", lambda: "context")
    monkeypatch.setattr(ts.server, "memory_quality_report", lambda sample_limit=5: "quality")
    monkeypatch.setattr(ts.server, "master_status", lambda limit=20: "agents")
    monkeypatch.setattr(ts.server, "diagnostics", lambda: "diagnostics")
    ts.CHAT_EVENTS[:] = [{"role": "user/message", "content": "hello"}]
    ts.LAST_RUN_SOURCE = "last answer"

    out = ts._handle_slash(
        "/dump bug",
        messages=[{"role": "user", "content": "/quality"}],
    )

    assert out.startswith("dumped chat/debug log to ")
    path = out.split(" to ", 1)[1]
    text = open(path, encoding="utf-8").read()
    assert "== messages ==" in text
    assert "/quality" in text
    assert "last answer" in text


def test_run_prompt_passes_context_size(monkeypatch):
    seen = {}
    monkeypatch.setattr(ts.server, "parse_interaction_id", lambda out: None)
    monkeypatch.setattr(ts, "_strip_footer", lambda out: out)

    def fake_answer(
        prompt,
        history,
        trace=False,
        strict=None,
        tier=None,
        context_size="",
        session="",
        project="",
    ):
        seen["context_size"] = context_size
        return "ok"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)

    assert ts._run_prompt("hi", context_size="1m") == "ok"
    assert seen["context_size"] == "1m"


def test_run_prompt_passes_session_and_project(monkeypatch):
    seen = {}
    monkeypatch.setattr(ts.server, "parse_interaction_id", lambda out: None)
    monkeypatch.setattr(ts, "_strip_footer", lambda out: out)

    def fake_answer(
        prompt,
        history,
        trace=False,
        strict=None,
        tier=None,
        context_size="",
        session="",
        project="",
    ):
        seen["session"] = session
        seen["project"] = project
        return "ok"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)

    assert ts._run_prompt("hi", session="chat-1", project="app") == "ok"
    assert seen == {"session": "chat-1", "project": "app"}


def test_cot_slash_is_denied(monkeypatch):
    monkeypatch.setattr(
        ts.server,
        "admin_private_chain_of_thought",
        lambda token="": "DENIED: no",
    )

    assert ts._handle_slash("/cot") == "DENIED: no"


def test_login_slash_stores_token(monkeypatch):
    monkeypatch.setattr(
        ts.server,
        "admin_login",
        lambda username, password: "login ok\ntoken: abc123",
    )
    monkeypatch.setattr(ts.server, "_admin_account_from_token", lambda token: {"username": "u"})
    ts.CURRENT_TOKEN = ""

    out = ts._handle_slash("/login user password123")

    assert "login ok" in out
    assert ts.CURRENT_TOKEN == "abc123"


def test_file_slash_commands_route_to_server(monkeypatch):
    monkeypatch.setattr(ts.server, "file_find", lambda **kwargs: "found")
    monkeypatch.setattr(ts.server, "file_read", lambda **kwargs: "read")
    monkeypatch.setattr(ts.server, "file_delete", lambda **kwargs: "dry delete")

    assert ts._handle_slash("/files *.py") == "found"
    assert ts._handle_slash("/read README.md") == "read"
    assert ts._handle_slash("/delete README.md") == "dry delete"


def test_artifact_and_game_slash_commands_route_to_server(monkeypatch):
    calls = []
    monkeypatch.setattr(
        ts.server,
        "artifact_generate",
        lambda **kwargs: calls.append(("asset", kwargs)) or "asset ok",
    )
    monkeypatch.setattr(
        ts.server,
        "game_reference_suite",
        lambda **kwargs: calls.append(("forge", kwargs)) or "forge ok",
    )
    monkeypatch.setattr(
        ts.server,
        "game_generate_and_test",
        lambda **kwargs: calls.append(("game", kwargs)) or "game ok",
    )
    monkeypatch.setattr(
        ts.server,
        "game_generation_campaign",
        lambda **kwargs: calls.append(("fleet", kwargs)) or "fleet ok",
    )

    assert ts._handle_slash("/asset brand-kit cobalt logo and notification sound") == "asset ok"
    assert ts._handle_slash("/forge smoke-suite") == "forge ok"
    assert ts._handle_slash("/game cpp 3d cavern | explore a crystal cavern") == "game ok"
    assert ts._handle_slash("/gamefleet demos | create compact arcade games") == "fleet ok"
    assert ts._handle_slash(
        "/gamefleet iso | create dungeon games | cpp | 2.5d"
    ) == "fleet ok"
    assert calls == [
        ("asset", {"name": "brand-kit", "brief": "cobalt logo and notification sound"}),
        ("forge", {"name": "smoke-suite"}),
        (
            "game",
            {
                "name": "cavern",
                "concept": "explore a crystal cavern",
                "language": "cpp",
                "dimension": "3d",
            },
        ),
        ("fleet", {"name": "demos", "concept": "create compact arcade games"}),
        (
            "fleet",
            {
                "name": "iso", "concept": "create dungeon games",
                "language": "cpp", "dimension": "2.5d",
            },
        ),
    ]


def test_write_slash_requires_path_and_text():
    assert ts._handle_slash("/write onlypath").startswith("usage:")


def test_http_session_state_is_scoped_and_blank_is_ephemeral():
    ts._HTTP_SESSION_STATES.clear()
    alice = {"account": {"username": "alice"}, "api_key": False}
    bob = {"account": {"username": "bob"}, "api_key": False}

    alice_first = ts._http_conversation_state(alice, "chat")
    alice_again = ts._http_conversation_state(alice, "chat")
    bob_state = ts._http_conversation_state(bob, "chat")
    blank_first = ts._http_conversation_state(alice, "")
    blank_again = ts._http_conversation_state(alice, "")

    assert alice_first is alice_again
    assert alice_first is not bob_state
    assert blank_first is not blank_again


def test_concurrent_turn_results_keep_iids_request_local(monkeypatch):
    barrier = threading.Barrier(2)
    ids = {"alpha": "aaa111", "beta": "bbb222"}

    def fake_answer(prompt, history, **kwargs):
        barrier.wait(timeout=3)
        return "answer-%s\n\n[interaction_id: %s]" % (prompt, ids[prompt])

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)
    alpha_state = ts.ConversationState()
    beta_state = ts.ConversationState()

    def run(prompt, state):
        return ts._run_prompt(
            prompt, history=[], state=state, return_result=True
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        alpha_future = pool.submit(run, "alpha", alpha_state)
        beta_future = pool.submit(run, "beta", beta_state)
        alpha = alpha_future.result(timeout=5)
        beta = beta_future.result(timeout=5)

    assert alpha.iid == "aaa111"
    assert beta.iid == "bbb222"
    assert alpha_state.last_iid == "aaa111"
    assert beta_state.last_iid == "bbb222"
    assert ts._chat_completion_object(
        alpha.content, iid=alpha.iid
    )["id"] == "chatcmpl-aaa111"
    assert ts._chat_completion_object(
        beta.content, iid=beta.iid
    )["id"] == "chatcmpl-bbb222"


def test_state_run_never_uses_legacy_global_response(monkeypatch):
    monkeypatch.setattr(
        ts, "LAST_RUN_SOURCE", "```python\nprint('wrong-global')\n```"
    )
    monkeypatch.setattr(
        ts, "LAST_RESPONSE", "```python\nprint('wrong-global')\n```"
    )
    state = ts.ConversationState()
    seen = {}

    def fake_run(code, language="python", timeout=8):
        seen["code"] = code
        return {
            "ok": True, "stdout": "ok", "stderr": "",
            "timeout": timeout, "returncode": 0,
        }

    monkeypatch.setattr(ts.code_runner, "run_code", fake_run)
    monkeypatch.setattr(ts.code_runner, "format_result", lambda result: "ok")
    messages = [{
        "role": "assistant",
        "content": "```python\nprint('right-request')\n```",
    }]

    out = ts._handle_slash("/run", messages=messages, state=state)

    assert out.endswith("[ran OK]")
    assert "right-request" in seen["code"]
    assert "wrong-global" not in seen["code"]


def test_feedback_consumes_only_its_conversation_iid(monkeypatch):
    alpha = ts.ConversationState(last_iid="aaa111")
    beta = ts.ConversationState(last_iid="bbb222")
    recorded = []
    monkeypatch.setattr(
        ts.server,
        "record_outcome",
        lambda iid, signal: recorded.append((iid, signal)) or "recorded",
    )

    assert ts._handle_slash("/pass", state=alpha) == "recorded"
    assert recorded == [("aaa111", "tests_passed")]
    assert alpha.last_iid is None
    assert beta.last_iid == "bbb222"


def test_chat_events_are_isolated_per_conversation():
    alpha = ts.ConversationState()
    beta = ts.ConversationState()

    ts._record_chat("user", "alpha", state=alpha)
    ts._record_chat("user", "beta", state=beta)

    assert alpha.events == [{"role": "user/message", "content": "alpha"}]
    assert beta.events == [{"role": "user/message", "content": "beta"}]
