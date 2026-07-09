import trilobite_serve as ts


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
    out = ts._handle_slash("/run 12")

    assert "timeout: 12s" in out
    assert "slow smoke" in out


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
    assert calls == [{"task": "build it", "mode": "delegate"}]


def test_agents_slash_returns_master_status(monkeypatch):
    monkeypatch.setattr(ts.server, "master_status", lambda: "agents live")

    assert ts._handle_slash("/agents") == "agents live"


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


def test_run_prompt_passes_context_size(monkeypatch):
    seen = {}
    monkeypatch.setattr(ts.server, "parse_interaction_id", lambda out: None)
    monkeypatch.setattr(ts, "_strip_footer", lambda out: out)

    def fake_answer(prompt, history, trace=False, strict=None, tier=None, context_size=""):
        seen["context_size"] = context_size
        return "ok"

    monkeypatch.setattr(ts.server, "answer_with_history", fake_answer)

    assert ts._run_prompt("hi", context_size="1m") == "ok"
    assert seen["context_size"] == "1m"


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


def test_write_slash_requires_path_and_text():
    assert ts._handle_slash("/write onlypath").startswith("usage:")
