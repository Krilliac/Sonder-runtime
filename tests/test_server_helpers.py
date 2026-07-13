import importlib
import threading

import memory_store
import server


def setup_function():
    server.master_orchestrator.reset_for_tests()


def test_with_footer_and_parse_roundtrip():
    out = server.with_footer("here is code", "abc123def4567890")
    assert out.endswith("[interaction_id: abc123def4567890]")
    assert server.parse_interaction_id(out) == "abc123def4567890"


def test_parse_none_when_absent():
    assert server.parse_interaction_id("just some text") is None


def test_agent_tool_help_advertises_strict_humanoid_artifact_contract():
    help_text = server._agent_tool_help()

    assert '"min_joints": 17' in help_text
    assert '"min_animation_sequences": 2' in help_text
    assert '"require_humanoid_rig": true' in help_text
    assert '"require_morph_normals": true' in help_text
    assert '"require_morph_tangents": true' in help_text
    assert '"required_animation_clips": ["Idle", "Walk", "Run"' in help_text


def test_resolve_sonder_falls_back(monkeypatch):
    # no alias present -> immutable base coder, not mutable policy model
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    assert server.resolve_sonder_model() == server.LOCAL_CODE_MODEL


def test_resolve_sonder_prefers_alias(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "sonder:latest"}]})
    assert server.resolve_sonder_model() == server.SONDER_STABLE_ALIAS


def test_resolve_sonder_soft_fails_when_ollama_down(monkeypatch):
    def boom(path):
        raise Exception("ollama down")
    monkeypatch.setattr(server, "_get", boom)
    assert server.resolve_sonder_model() == server.LOCAL_CODE_MODEL


def test_resolve_sonder_strict_true_prefers_alias(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "sonder:latest"}]})
    assert server.resolve_sonder_model(strict=True) == server.SONDER_STABLE_ALIAS


def test_resolve_sonder_strict_true_alias_absent_returns_none(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    assert server.resolve_sonder_model(strict=True) is None


def test_resolve_sonder_strict_false_alias_absent_falls_back(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    assert server.resolve_sonder_model(strict=False) == server.LOCAL_CODE_MODEL


def test_resolve_sonder_rejects_non_latest_sonder_tag(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get",
        lambda path: {"models": [{"name": "sonder:experimental"}]},
    )

    assert server.resolve_sonder_model(strict=True) is None
    assert server.resolve_sonder_model(strict=False) == server.LOCAL_CODE_MODEL


def test_resolve_sonder_policy_alias_cannot_shadow_base_fallback(monkeypatch):
    monkeypatch.setitem(server.TIERS, "code", server.SONDER_STABLE_ALIAS)
    monkeypatch.setattr(
        server,
        "_get",
        lambda path: {"models": [{"name": "sonder:experimental"}]},
    )

    assert server.resolve_sonder_model(strict=False) == server.LOCAL_CODE_MODEL
    assert server.resolve_sonder_model(strict=False) != server.TIERS["code"]


def test_resolve_sonder_accepts_exact_alias_from_model_field(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get",
        lambda path: {"models": [{"model": " SONDER:latest "}]},
    )

    assert server.resolve_sonder_model(strict=True) == server.SONDER_STABLE_ALIAS


def test_resolve_sonder_ignores_malformed_tag_entries(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get",
        lambda path: {
            "models": [None, "sonder:latest", 7, {"name": "sonder:preview"}],
        },
    )

    assert server.resolve_sonder_model(strict=True) is None


def test_sonder_strict_true_errors_when_alias_missing_before_any_ollama_call(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})

    def boom_post(path, payload):
        raise AssertionError("must not call Ollama when strict + alias missing")
    monkeypatch.setattr(server, "_post", boom_post)

    out = server.sonder("hi", strict=True)
    assert "not found" in out


def test_should_learn_defaults_to_local_tiers():
    assert server._should_learn("fast", True) is True
    assert server._should_learn("code", True) is True
    assert server._should_learn("general", True) is True
    assert server._should_learn("cloud-code", True) is False
    assert server._should_learn("cloud-general", True) is False
    # learn=False still opts out.
    assert server._should_learn("code", False) is False
    assert server._should_learn("cloud-code", False) is False


def test_should_learn_honors_learn_tiers(monkeypatch):
    monkeypatch.setattr(server, "LEARN_TIERS", {"code"})
    assert server._should_learn("code", True) is True
    assert server._should_learn("cloud-code", True) is False


def test_make_generate_adds_local_runtime_options(monkeypatch):
    seen = {}

    def fake_post(path, payload):
        seen["path"] = path
        seen["payload"] = payload
        return {"message": {"content": "ok"}}

    monkeypatch.setenv("SONDER_NUM_THREAD", "12")
    monkeypatch.setenv("SONDER_NUM_GPU", "99")
    monkeypatch.setenv("SONDER_NUM_BATCH", "256")
    monkeypatch.setattr(server, "_post", fake_post)

    gen = server._make_generate("local-model", "system", 0.3, 77, 4096)
    assert gen("hello") == "ok"
    assert gen.last_usage["tokens_in"] > 0
    assert gen.last_usage["tokens_out"] == 1
    assert gen.last_usage["token_source"] == "estimated"
    assert seen["path"] == "/api/chat"
    assert seen["payload"]["keep_alive"] == server.KEEP_ALIVE
    assert seen["payload"]["options"] == {
        "temperature": 0.3,
        "num_predict": 77,
        "num_ctx": 4096,
        "num_thread": 12,
        "num_gpu": 99,
        "num_batch": 256,
    }


def test_local_model_options_clamps_native_context(monkeypatch):
    monkeypatch.setenv("SONDER_NATIVE_CONTEXT_MAX", "256k")

    opts = server._local_model_options(0.2, 10, 1000000)

    assert opts["num_ctx"] == 256000


def test_make_generate_cloud_omits_local_runtime_options(monkeypatch):
    seen = {}

    def fake_post(path, payload):
        seen["payload"] = payload
        return {"message": {"content": "ok"}}

    monkeypatch.setenv("SONDER_NUM_THREAD", "12")
    monkeypatch.setenv("SONDER_NUM_GPU", "99")
    monkeypatch.setenv("SONDER_NUM_BATCH", "256")
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(server, "_post", fake_post)

    gen = server._make_generate("cloud-model", "", 0.4, 88, 8192, cloud=True)
    assert gen("hello") == "ok"
    assert "keep_alive" not in seen["payload"]
    assert seen["payload"]["options"] == {"temperature": 0.4, "num_predict": 88}


def test_make_generate_captures_ollama_token_counts(monkeypatch):
    def fake_post(path, payload):
        return {
            "message": {"content": "ok"},
            "prompt_eval_count": 17,
            "eval_count": 9,
        }

    monkeypatch.setattr(server, "_post", fake_post)

    gen = server._make_generate("local-model", "", 0.1, 20, 2048)
    assert gen("hello") == "ok"
    assert gen.last_usage == {
        "tokens_in": 17,
        "tokens_out": 9,
        "token_source": "ollama",
    }


def test_serve_target_cloud_tier_requires_opt_in(monkeypatch):
    monkeypatch.delenv("SONDER_ALLOW_CLOUD", raising=False)
    model, cloud, augment, label = server._serve_target("cloud-code", None)
    assert model is None
    assert cloud is True
    assert augment is False
    assert label == "cloud-disabled"


def test_serve_target_cloud_tier_is_clean_teacher_when_enabled(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    # Cloud tier: real cloud model, cloud=True, augment=False (clean), labeled by tier.
    model, cloud, augment, label = server._serve_target("cloud-code", None)
    assert model == server.TIERS["cloud-code"]
    assert cloud is True
    assert augment is False
    assert label == "cloud-code"


def test_serve_target_treats_code_as_local():
    model, cloud, augment, label = server._serve_target("code", None)
    assert model == server.TIERS["code"]
    assert cloud is False
    assert augment is True
    assert label == "code"


def test_serve_target_cloud_detection_helper_detects_cloud_model_name(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setitem(server.TIERS, "code", "qwen3-coder:480b-cloud")
    model, cloud, augment, label = server._serve_target("code", None)
    assert model == "qwen3-coder:480b-cloud"
    assert cloud is True
    assert augment is True
    assert label == "code"


def test_serve_target_local_general_tier_answers_clean():
    # A non-code local tier runs that model but does not augment (only 'code' is student).
    model, cloud, augment, label = server._serve_target("general", None)
    assert model == server.TIERS["general"]
    assert cloud is False
    assert augment is False
    assert label == "general"


def test_serve_target_unknown_model_is_rejected():
    model, cloud, augment, label = server._serve_target("gpt-4o", None)
    assert label is None


def test_canonical_learn_tier_maps_student_to_code():
    assert server._canonical_learn_tier("sonder") == "code"
    assert server._canonical_learn_tier("cloud-code") == "cloud-code"
    assert server._canonical_learn_tier("general") == "general"


def test_sonder_tool_unknown_tier_errors_before_ollama(monkeypatch):
    def boom_post(path, payload):
        raise AssertionError("must not call Ollama for an unknown tier")
    monkeypatch.setattr(server, "_post", boom_post)
    out = server.sonder("hi", tier="does-not-exist")
    assert "unknown tier" in out


def test_answer_with_history_unknown_model_errors_before_ollama(monkeypatch):
    def boom_post(path, payload):
        raise AssertionError("must not call Ollama for an unknown model")
    monkeypatch.setattr(server, "_post", boom_post)
    out = server.answer_with_history("hi", None, tier="gpt-9-turbo")
    assert "unknown model" in out


def test_serve_target_default_is_local_student(monkeypatch):
    monkeypatch.setattr(server, "_get",
                        lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    for name in ("", "sonder", "local", None):
        model, cloud, augment, label = server._serve_target(name, None)
        assert model == server.LOCAL_CODE_MODEL
        assert cloud is False
        assert augment is True
        assert label == "sonder"


def test_serve_target_strict_uses_explicit_stable_alias(monkeypatch):
    monkeypatch.setattr(
        server,
        "_get",
        lambda path: {"models": [{"name": "sonder:latest"}]},
    )

    model, cloud, augment, label = server._serve_target("sonder", True)

    assert model == server.SONDER_STABLE_ALIAS
    assert cloud is False
    assert augment is True
    assert label == "sonder"


def test_sonder_stats_runs_against_empty_db(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "empty.db"))
    out = server.sonder_stats()
    assert isinstance(out, str)
    assert "lessons:" in out
    assert "tokens:" in out
    assert "token rows:" in out


def test_learning_health_is_structured_and_routed(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "learning.db"))
    monkeypatch.setattr(server.embeddings, "EXPECTED_DIMENSION", 1)
    conn = server._open_db()
    try:
        memory_store.log_interaction(
            conn, "i1", "task", "", "answer", "code"
        )
        memory_store.refresh_interaction_task_embedding(
            conn,
            "i1",
            server.embeddings.to_blob([1.0]),
            server.embeddings.EMBED_IDENTITY,
            revision=server.embeddings.EMBED_REVISION,
            dimension=1,
        )
        memory_store.record_outcome_row(conn, "i1", "tests_passed", 1.0)
        memory_store.add_lesson(
            conn,
            "lesson-one",
            "Verify the exact packaged payload before release.",
            server.embeddings.to_blob([1.0]),
            "i1",
            embedding_model=server.embeddings.EMBED_IDENTITY,
            embedding_revision=server.embeddings.EMBED_REVISION,
            embedding_dim=1,
        )
    finally:
        conn.close()

    data = server.learning_health_data()
    text = server.learning_health_status()

    assert data["status"] == "healthy"
    assert data["outcome_coverage_percent"] == 100.0
    assert data["grounded_lessons"] == 1
    assert "sonder learning health" in text
    assert server.control_command("/learning") == text
    assert server.control_command("/metrics") == text


def test_session_history_never_crosses_project_or_uses_shared_summary():
    conn = memory_store.connect(":memory:")
    memory_store.touch_session(conn, "default", project="project-a")
    memory_store.log_interaction(
        conn, "a", "task a", "", "PROJECT_A_PRIVATE", "sonder",
        session_id="default", project="project-a", project_explicit=True,
    )
    memory_store.log_interaction(
        conn, "b", "task b", "", "project b response", "sonder",
        session_id="default", project="project-b", project_explicit=True,
    )
    memory_store.update_session_summary(
        conn, "default", "PROJECT_A_SUMMARY_PRIVATE", "a",
    )

    history = server._session_history_messages(
        conn, "default", 12, project="project-b",
    )

    assert history == [
        {"role": "user", "content": "task b"},
        {"role": "assistant", "content": "project b response"},
    ]
    assert "PROJECT_A" not in repr(history)


def test_session_history_uses_a_project_keyed_summary(monkeypatch):
    conn = memory_store.connect(":memory:")
    memory_store.touch_session(conn, "default", project="project-b")
    for index in range(3):
        memory_store.log_interaction(
            conn, "b%d" % index, "task b%d" % index, "", "response b%d" % index,
            "sonder", session_id="default", project="project-b",
            project_explicit=True,
        )
    monkeypatch.setattr(
        server.summarizer, "summarize",
        lambda previous, pairs, generate: "PROJECT_B_SUMMARY",
    )

    history = server._session_history_messages(
        conn, "default", 1, project="project-b",
    )
    stored = memory_store.get_session_project_summary(
        conn, "default", "project-b",
    )

    assert history[0]["content"].endswith("PROJECT_B_SUMMARY")
    assert history[-1]["content"] == "response b2"
    assert stored == {
        "summary": "PROJECT_B_SUMMARY", "summarized_through": "b1",
    }


def test_context_health_reports_session_and_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(server, "SESSION_NUM_CTX", 100)
    monkeypatch.setattr(server, "MAX_TURNS", 2)
    conn = server._open_db()
    try:
        memory_store.touch_session(conn, "demo", project="proj")
        memory_store.update_session_summary(conn, "demo", "older summary text", "old-turn")
        memory_store.log_interaction(
            conn,
            "i1",
            "make a tiny game",
            "",
            "print('ok')",
            "code",
            session_id="demo",
            project="proj",
            project_explicit=True,
        )
        memory_store.add_lesson(
            conn, "lesson-one", "Prefer runnable snippets.", None, "i1"
        )
        memory_store.add_fact(conn, "fact-one", "proj", "Use the local app bundle.")
        memory_store.record_outcome_row(conn, "i1", "tests_passed", 1.0)
    finally:
        conn.close()

    data = server.context_health_data(session="demo", project="proj")

    assert data["session"] == "demo"
    assert data["project"] == "proj"
    assert data["live_turns"] == 1
    assert data["lessons"] == 1
    assert data["facts"] == 1
    assert data["outcomes"] == 1
    assert data["context_percent"] > 0
    assert data["context_bar"].startswith("[")
    assert data["native_context_limit"] <= data["context_limit"]
    assert data["context_mode"] in ("native", "virtual")


def test_context_health_formats_console_meter(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    out = server.context_health()
    assert "sonder context health" in out
    assert "context [" in out
    assert "native" in out
    assert "memory  [" in out


def test_set_context_size_selects_virtual_context(monkeypatch):
    monkeypatch.setenv("SONDER_NATIVE_CONTEXT_MAX", "256k")
    old = server.SESSION_NUM_CTX
    try:
        out = server.set_context_size("1m")
        assert server.SESSION_NUM_CTX == 1000000
        assert "mode: virtual" in out
        assert server._context_native() == 256000
    finally:
        server.SESSION_NUM_CTX = old


def test_control_command_routes_quality_before_model(monkeypatch):
    monkeypatch.setattr(server, "memory_quality_report", lambda: "quality report")

    assert server.control_command("/quality") == "quality report"


def test_control_command_routes_persisted_agent_retry(monkeypatch):
    monkeypatch.setattr(
        server,
        "master_retry",
        lambda agent_id, tier="": f"retry:{agent_id}:{tier}",
    )

    assert server.control_command("/agentretry master-old") == "retry:master-old:"
    assert server.control_command(
        "/agentretry master-old general",
    ) == "retry:master-old:general"


def test_control_command_routes_targeted_game_campaign(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "game_generation_campaign",
        lambda **kwargs: calls.append(kwargs) or "campaign",
    )

    out = server.control_command(
        "/gamefleet abyss | dungeon combat | c++ | 2.5d",
    )

    assert out == "campaign"
    assert calls == [{
        "name": "abyss", "concept": "dungeon combat",
        "language": "c++", "dimension": "2.5d",
    }]


def test_control_command_routes_weather_without_model(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "weather_lookup",
        lambda location: calls.append(location) or "weather result",
    )

    assert server.control_command("/weather Chicago, IL") == "weather result"
    assert server.control_command("/weather") == "usage: /weather <city/state or ZIP>"
    assert calls == ["Chicago, IL"]


def test_control_command_dump_writes_file(monkeypatch, tmp_path):
    monkeypatch.setattr(server.sonder_paths, "default_home", lambda: tmp_path)
    monkeypatch.setattr(server, "context_health", lambda session="", project="": "context")
    monkeypatch.setattr(server, "memory_quality_report", lambda sample_limit=5: "quality")
    monkeypatch.setattr(server, "master_status", lambda limit=20: "agents")
    monkeypatch.setattr(server, "diagnostics", lambda: "diagnostics")

    out = server.control_command(
        "/dump bug",
        history=[{"role": "assistant", "content": "```python\nprint('kept')\n```"}],
        session="none",
        project="none",
    )

    assert out.startswith("dumped chat/debug log to ")
    assert "last runnable block retained for /run" in out
    path = out.splitlines()[0].split(" to ", 1)[1]
    text = open(path, encoding="utf-8").read()
    assert "== messages ==" in text
    assert "print('kept')" in text


def test_control_command_dump_never_appends_another_projects_turns(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "memory.db"))
    monkeypatch.setattr(server.sonder_paths, "default_home", lambda: tmp_path)
    monkeypatch.setattr(server, "context_health", lambda **kwargs: "context")
    monkeypatch.setattr(server, "memory_quality_report", lambda **kwargs: "quality")
    monkeypatch.setattr(server, "master_status", lambda **kwargs: "agents")
    monkeypatch.setattr(server, "diagnostics", lambda: "diagnostics")
    conn = server._open_db()
    memory_store.touch_session(conn, "shared", project="project-a")
    memory_store.log_interaction(
        conn, "a", "PRIVATE_A_TASK", "", "PRIVATE_A_RESPONSE", "sonder",
        session_id="shared", project="project-a", project_explicit=True,
    )
    memory_store.log_interaction(
        conn, "b", "project b task", "", "project b response", "sonder",
        session_id="shared", project="project-b", project_explicit=True,
    )
    conn.close()

    out = server.control_command(
        "/dump scoped", session="shared", project="project-b",
    )
    path = out.splitlines()[0].split(" to ", 1)[1]
    text = open(path, encoding="utf-8").read()

    assert "project b response" in text
    assert "PRIVATE_A" not in text


def test_control_command_run_uses_history(monkeypatch):
    seen = {}

    def fake_run(code, language="python", timeout=8):
        seen["code"] = code
        seen["language"] = language
        seen["timeout"] = timeout
        return {"ok": True, "stdout": "ok", "stderr": "", "timeout": timeout, "returncode": 0}

    monkeypatch.setattr(server.code_runner, "run_code", fake_run)
    monkeypatch.setattr(server.code_runner, "format_result", lambda result: result["stdout"])

    out = server.control_command(
        "/run 9",
        history=[{"role": "assistant", "content": "```cpp\nint main(){return 0;}\n```"}],
    )

    assert out.endswith("[ran OK]")
    assert seen == {"code": "int main(){return 0;}", "language": "cpp", "timeout": 9}


def test_sonder_slash_command_does_not_call_model(monkeypatch):
    monkeypatch.setattr(server, "context_health", lambda: "context health")
    monkeypatch.setattr(server, "_serve_target", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model should not resolve")))

    assert server.sonder("/context") == "context health"


def test_preference_command_learns_and_lists(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "prefs.db"))

    out = server.preference_command("I prefer short direct answers")
    assert "Learned preference" in out
    assert "User prefers short direct answers." in server.preferences_status()


def test_activity_tracks_file_line_deltas(monkeypatch, tmp_path):
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: tmp_path)
    server.activity_tracker.reset_for_tests()

    with server.activity_tracker.response_span("test", "create a file"):
        out = server.file_write("notes.txt", "one\ntwo\n", mode="create")

    latest = server.activity_tracker.snapshot()["latest"]
    assert "file write" in out
    assert latest["file_creates"] == 1
    assert latest["lines_added"] == 2
    assert latest["files"][0]["path"].endswith("notes.txt")


def test_completed_surface_replaces_inflight_activity_snapshot():
    server.activity_tracker.reset_for_tests()

    with server.activity_tracker.response_span("http", "/inventory") as response:
        interim = server._append_activity("inventory result")
        assert " running " in interim

    final = server._append_activity(interim, response=response, replace=True)

    assert final.count("=== ACTIVITY (observable work) ===") == 1
    assert " complete " in final
    assert " running " not in final


def test_completed_activity_keeps_interaction_footer_last_and_parseable():
    server.activity_tracker.reset_for_tests()

    with server.activity_tracker.response_span("terminal", "hello") as response:
        interim = server.with_footer("answer", "abc123def4567890")

    final = server._append_activity(interim, response=response, replace=True)

    assert " complete " in final
    assert final.endswith("[interaction_id: abc123def4567890]")
    assert server.parse_interaction_id(final) == "abc123def4567890"


def test_memory_search_includes_preferences(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "prefs.db"))
    server.learn_preference("User prefers MSVC for C++ examples.")

    out = server.memory_search("MSVC")

    assert "preferences (1):" in out
    assert "User prefers MSVC" in out


def test_improvement_report_flags_ungrounded_learning(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    conn = server._open_db()
    try:
        memory_store.log_interaction(
            conn,
            "i1",
            "build a parser",
            "",
            "use appropriate handling",
            "code",
        )
    finally:
        conn.close()

    report = server.improvement_report_data()
    text = server.format_improvement_report(report)

    assert report["interactions"] == 1
    assert report["outcomes"] == 0
    assert any(i["area"] == "learning" for i in report["issues"])
    assert "sonder improvement report" in text
    assert "next improvements:" in text


def test_improvement_report_honors_cloud_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")

    report = server.improvement_report_data()

    assert report["cloud_allowed"] is True
    assert not any(i["area"] == "deployment" for i in report["issues"])


def test_improvement_report_flags_failed_closed_mcp_refresh(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    state = server.mcp_runtime_data()
    state.update({
        "status": "error",
        "source_changed": True,
        "last_error": "SyntaxError: invalid syntax",
    })
    monkeypatch.setattr(server, "mcp_runtime_data", lambda: dict(state))

    report = server.improvement_report_data()

    issue = next(item for item in report["issues"] if item["area"] == "runtime")
    assert issue["severity"] == "high"
    assert "failed closed" in issue["title"]
    assert report["mcp_runtime"]["last_error"].startswith("SyntaxError")
    assert "mcp: error" in server.format_improvement_report(report)


def test_master_orchestrate_asks_for_execution_mode():
    out = server.master_orchestrate("build a parser", mode="ask", agents=2)

    assert "Choose execution mode" in out
    assert "inline" in out
    assert "delegate" in out


def test_master_orchestrate_ask_reports_widened_agent_cap(monkeypatch):
    monkeypatch.setenv("SONDER_MAX_AGENTS", "16")

    out = server.master_orchestrate("build a parser", mode="ask", agents=99)

    assert "queue 16 agent(s)" in out
    assert "safe worker slot(s)" in out


def test_master_capacity_and_cancel_tools(monkeypatch):
    server.master_orchestrator.reset_for_tests()
    gib = 1024 ** 3
    monkeypatch.setattr(
        server.master_orchestrator,
        "capacity",
        lambda requested=None: {
            "logical_cpus": 16,
            "total_memory_bytes": 16 * gib,
            "available_memory_bytes": 4 * gib,
            "agent_ceiling": 32,
            "requested_agents": requested or 32,
            "worker_slots": 2,
            "automatic_worker_slots": 2,
            "source": "auto",
            "ram_reserve_bytes": int(1.5 * gib),
            "ram_per_worker_bytes": int(1.25 * gib),
        },
    )

    capacity = server.master_capacity(32)
    master_id = server.master_orchestrator._new_agent("master", "long task")
    assert server.master_orchestrator._start_agent(
        master_id, "calling model", in_model_call=True,
    )
    canceled = server.master_cancel(master_id[:12])

    assert "concurrent worker slots: 2" in capacity
    assert "matched: 1" in canceled
    assert "active model calls awaiting return: 1" in canceled
    assert "running agents signalled: 1" in canceled
    assert "cannot be force-killed" in canceled


def test_orchestrator_worker_propagates_activity_into_worker_thread(monkeypatch):
    calls = []

    def fake_offload(**kwargs):
        calls.append(kwargs)
        server.activity_tracker.record_model_call(
            model="fake-model", prompt_chars=len(kwargs["prompt"]),
            tokens_in=4, tokens_out=2,
        )
        return "worker output"

    monkeypatch.setattr(server, "_offload_impl", fake_offload)
    server.activity_tracker.reset_for_tests()
    with server.activity_tracker.response_span("master", "delegate") as response:
        worker = server._orchestrator_worker("code")
        thread = threading.Thread(target=lambda: worker("subtask"))
        thread.start()
        thread.join(2)

        assert not thread.is_alive()
        assert calls[0]["tier"] == "code"
        assert response["model_calls"] == 1
        assert response["tokens_in"] == 4
        assert response["tokens_out"] == 2


def test_orchestrator_agent_worker_raises_host_generated_errors(monkeypatch):
    monkeypatch.setattr(
        server,
        "_agent_impl",
        lambda *args, **kwargs: "ERROR: model decision failed",
    )

    worker = server._orchestrator_agent_worker("code")

    try:
        worker("inspect repository")
    except RuntimeError as error:
        assert "model decision failed" in str(error)
    else:
        raise AssertionError("host-generated agent error was treated as success")


def test_repo_master_uses_cancel_aware_worker_and_persists_host_failure(monkeypatch):
    calls = []

    def fail_agent(*args, **kwargs):
        calls.append(kwargs)
        return "ERROR: repository agent failed"

    monkeypatch.setattr(server, "_agent_impl", fail_agent)

    out = server.master_orchestrate(
        "Repository: D:\\demo. Inspect current files.",
        mode="inline",
    )
    snap = server.master_orchestrator.snapshot()
    master = next(row for row in snap["agents"] if row["role"] == "master")

    assert "repository agent failed" in out
    assert callable(calls[0]["cancel_check"])
    assert master["status"] == "failed"


def test_non_learning_offload_records_model_usage(monkeypatch):
    monkeypatch.setattr(
        server,
        "_post",
        lambda *args, **kwargs: {
            "message": {"content": "plain output"},
            "prompt_eval_count": 9,
            "eval_count": 3,
        },
    )
    server.activity_tracker.reset_for_tests()

    with server.activity_tracker.response_span("offload", "plain") as response:
        output = server.offload("plain", tier="fast", learn=False)

        assert output == "plain output"
        assert response["model_calls"] == 1
        assert response["tokens_in"] == 9
        assert response["tokens_out"] == 3


def test_activity_tracker_hot_reload_preserves_open_response_span():
    server.activity_tracker.reset_for_tests()

    with server.activity_tracker.response_span("reload", "keep state") as response:
        response_id = server.activity_tracker.current_response_id()
        reloaded = importlib.reload(server.activity_tracker)
        reloaded.record_model_call(model="after-reload", tokens_in=2, tokens_out=1)

        assert reloaded.current_response_id() == response_id
        assert response["model_calls"] == 1
        assert response["tokens_in"] == 2


def test_master_orchestrate_accepts_common_delegate_typo(monkeypatch):
    monkeypatch.setattr(
        server.master_orchestrator,
        "run_delegated",
        lambda *args, **kwargs: {
            "master_id": "master-test",
            "agents": ["agent-one", "agent-two"],
            "worker_slots": 1,
            "output": "merged",
        },
    )

    out = server.master_orchestrate("build it", mode="delagte", agents=2)

    assert "master orchestration complete" in out
    assert "agents=2" in out
    assert "worker slots used: 1" in out


def test_master_routes_explicit_game_build_to_grounded_forge(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "_master_grounded_build",
        lambda task, mode, tier, intent, retry_of="": (
            calls.append((task, mode, tier, intent, retry_of)) or "grounded game"
        ),
    )

    out = server.master_orchestrate(
        "Create a C++ 2.5D isometric RPG game with in-house assets.",
        mode="delegate",
    )

    assert out == "grounded game"
    assert calls[0][1:3] == ("delegate", "code")
    assert calls[0][3]["kind"] == "game"
    assert calls[0][3]["language"] == "cpp"
    assert calls[0][3]["dimension"] == "2.5d"


def test_master_grounded_game_build_creates_verified_output(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "game_generate_and_test",
        lambda **kwargs: calls.append(kwargs) or "generated game: PASS\nroot: C:/games/demo",
    )
    intent = server.creative_router.classify(
        "Create a Python 2D dungeon game with generated sprites.",
        mode="delegate",
    )

    out = server._master_grounded_build(
        intent["concept"], "delegate", "code", intent,
    )

    assert "master grounded build complete" in out
    assert "persistent files + deterministic verification" in out
    assert "generated game: PASS" in out
    assert calls[0]["language"] == "python"
    assert calls[0]["dimension"] == "2d"


def test_master_grounded_campaign_preserves_explicit_targets(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "game_generation_campaign",
        lambda **kwargs: calls.append(kwargs) or "campaign: PASS",
    )
    intent = server.creative_router.classify(
        "Build 3 C++ 2.5D dungeon games as a fleet.", mode="fleet",
    )

    out = server._master_grounded_build(
        intent["concept"], "fleet", "code", intent,
    )

    assert "campaign: PASS" in out
    assert calls[0]["language"] == "cpp"
    assert calls[0]["dimension"] == "2.5d"
    assert calls[0]["total"] == 3


def test_master_does_not_hijack_game_questions(monkeypatch):
    monkeypatch.setattr(server, "_offload_impl", lambda prompt, **kwargs: "ordinary answer")

    out = server.master_orchestrate("How do I build a C++ game?", mode="inline")

    assert out == "ordinary answer"


def test_master_retry_replays_persisted_task_with_local_safe_default(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server.master_orchestrator,
        "recovery_candidate",
        lambda selector: {
            "id": "master-old",
            "status": "interrupted",
            "task": "finish the review",
            "mode": "fleet",
            "requested_agents": 12,
            "tier": "cloud-code",
        },
    )
    monkeypatch.setattr(
        server,
        "master_orchestrate",
        lambda **kwargs: calls.append(kwargs) or "retry complete",
    )

    out = server.master_retry("master-old")

    assert "persisted master retry" in out
    assert "retry complete" in out
    assert calls == [{
        "task": "finish the review",
        "mode": "fleet",
        "agents": 12,
        "tier": "code",
        "learn": False,
        "retry_of": "master-old",
    }]


def test_master_retry_rejects_completed_master(monkeypatch):
    monkeypatch.setattr(
        server.master_orchestrator,
        "recovery_candidate",
        lambda selector: {
            "id": "master-done", "status": "done", "task": "already done",
        },
    )

    assert "only interrupted/failed/cancelled" in server.master_retry("master-done")


def test_master_orchestrate_delegates_and_audits(monkeypatch):
    calls = []
    call_options = []

    def fake_offload(prompt, **kwargs):
        calls.append(prompt)
        call_options.append(kwargs)
        if "Audit these delegated outputs" in prompt or "master orchestrator" in prompt.lower():
            return "audited merge"
        return "agent output"

    monkeypatch.setattr(server, "_offload_impl", fake_offload)

    out = server.master_orchestrate("find risks", mode="delegate", agents=2)

    assert "master orchestration complete" in out
    assert "audited merge" in out
    assert len(calls) == 3
    assert sorted(options["timeout"] for options in call_options) == [120, 150, 150]
    assert "active agents: 0" in server.master_status()
    assert "latest completed master result:\naudited merge" in server.master_status()


def test_master_orchestrate_uses_tool_agent_for_repo_inspection(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server,
        "_agent_impl",
        lambda prompt, **kwargs: (
            calls.append((prompt, kwargs)) or
            "grounded agent output\n\n=== TOOL EVIDENCE ===\nstep 1 tool=file_read\nsource"
        ),
    )
    monkeypatch.setattr(server, "_offload_impl", lambda prompt, **kwargs: "audited merge")

    out = server.master_orchestrate(
        "Repository: D:\\SparkEngine. Review current uncommitted files using local file-reading tools.",
        mode="delegate",
        agents=4,
    )

    assert "audited merge" in out
    assert len(calls) == 4
    assert all(options["require_file_evidence"] for _, options in calls)
    assert all(options["read_only"] for _, options in calls)
    assert all(options["include_evidence"] for _, options in calls)


def test_admin_register_login_and_cot_denial(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "admin.db"))

    registered = server.admin_register("owner", "password123")
    login = server.admin_login("owner", "password123")

    assert "role=admin" in registered
    assert "token:" in login
    token = login.split("token: ", 1)[1].strip()
    assert "owner role=admin" in server.admin_whoami(token)
    assert "hidden private chain-of-thought cannot be exposed" in (
        server.admin_private_chain_of_thought(token)
    )


def test_admin_accounts_requires_admin_token(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "admin.db"))

    assert server.admin_accounts("").startswith("ERROR:")
    server.admin_register("owner", "password123")
    login = server.admin_login("owner", "password123")
    token = login.split("token: ", 1)[1].strip()

    assert "owner role=admin" in server.admin_accounts(token)


def test_file_tools_available_without_admin_inside_guarded_root(monkeypatch, tmp_path):
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: tmp_path)

    out = server.file_write("demo.txt", "hello")
    read = server.file_read("demo.txt")

    assert "file write" in out
    assert "hello" in read


def test_file_tools_reject_outside_root_without_approval(monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: root)

    out = server.file_read(str(outside))

    assert out.startswith("ERROR:")
    assert "outside allowed roots" in out


def test_file_tools_allow_extra_root_with_approval(monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "ok.txt"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("SONDER_FILE_APPROVAL_CODE", "let-me")

    out = server.file_read(
        str(target),
        approval="let-me",
        extra_roots=str(outside),
    )

    assert "ok" in out


def test_parallel_run_code_reports_mixed_results():
    jobs = '[{"name":"ok","code":"print(2+2)"},{"name":"fail","code":"raise ValueError(\\"x\\")"}]'
    out = server.parallel_run_code(jobs, max_workers=2, timeout=8)
    assert "parallel code jobs: 1/2 passed" in out
    assert "[PASS] ok" in out
    assert "[FAIL] fail" in out


def test_artifact_generate_formats_general_pack(monkeypatch):
    monkeypatch.setattr(
        server.assetgen,
        "generate_artifacts",
        lambda **kwargs: {
            "name": kwargs["name"], "dimension": "3d", "theme": "frost",
            "files": [{"path": "icon.png"}], "total_bytes": 99,
            "root": "C:/repo/artifacts/demo", "manifest": "C:/repo/artifacts/demo/manifest.json",
        },
    )

    out = server.artifact_generate("demo", "frosty logo and 3D model")

    assert "asset pack: demo" in out
    assert "3d / frost" in out


def test_game_generate_records_grounded_success(monkeypatch):
    project = {
        "language": "python", "dimension": "2d", "root": "C:/repo/game",
        "source": "C:/repo/game/game.py", "frame": "C:/repo/game/frame.ppm",
    }
    monkeypatch.setattr(server.game_forge, "prepare_project", lambda *a, **k: project)
    monkeypatch.setattr(server.game_forge, "generation_prompt", lambda *a, **k: "prompt")
    monkeypatch.setattr(
        server,
        "sonder",
        lambda *a, **k: (
            "```python\n# assets/tiles.png assets/hit.wav\n"
            "open('frame.ppm','wb').write(b'P6')\nprint('GAME_OK')\n```\n\n"
            "[interaction_id: abc123]"
        ),
    )
    monkeypatch.setattr(server.game_forge, "run_project", lambda *a, **k: {
        "ok": True, "output": "GAME_OK language=python dimension=2d",
        "source": project["source"], "frame": project["frame"],
    })
    records = []
    monkeypatch.setattr(server, "record_outcome", lambda iid, signal: records.append((iid, signal)) or "recorded")

    server.activity_tracker.reset_for_tests()
    with server.activity_tracker.response_span("game", "build") as activity:
        out = server.game_generate_and_test("demo", "arena", repair_rounds=0)

    assert "generated game: PASS" in out
    assert records == [("abc123", "tests_passed")]
    assert activity["tool_calls"] == 1
    assert activity["file_creates"] == 1


def test_forbidden_dependency_repair_note_uses_remediation(monkeypatch):
    project = {
        "language": "cpp", "dimension": "2d", "root": "C:/repo/game",
        "source": "C:/repo/game/game.cpp", "frame": "C:/repo/game/frame.ppm",
    }
    monkeypatch.setattr(server.game_forge, "prepare_project", lambda *a, **k: project)
    monkeypatch.setattr(server.game_forge, "generation_prompt", lambda *a, **k: "prompt")

    def no_reference(*a, **k):
        raise ValueError("no reference")

    monkeypatch.setattr(server.game_forge, "reference_source", no_reference)
    prompts = []

    def fake_sonder(prompt, **kwargs):
        prompts.append(prompt)
        return "```cpp\n#include <nlohmann/json.hpp>\nint main(){return 0;}\n```"

    monkeypatch.setattr(server, "sonder", fake_sonder)

    result = server._game_generate_result(
        "demo", "arena", "cpp", "2d", "arcane", 1, "code", 5, 1,
        use_reference_fallback=False,
    )

    assert result["ok"] is False
    assert len(prompts) == 2
    # The repair prompt must carry the actionable remediation, not just the
    # bare token list.
    assert "nlohmann" in prompts[1]
    assert "Remove every use of them" in prompts[1]
    assert "<fstream>" in prompts[1]


def test_repair_rounds_default_resolution():
    assert server._resolve_repair_rounds(None, "cpp") == 2
    assert server._resolve_repair_rounds(None, "c++") == 2
    assert server._resolve_repair_rounds(None, "python") == 1
    assert server._resolve_repair_rounds(None, "not-a-language") == 1
    assert server._resolve_repair_rounds(0, "cpp") == 0
    assert server._resolve_repair_rounds(5, "python") == 2


def test_cpp_default_gets_two_repair_rounds_end_to_end(monkeypatch):
    project = {
        "language": "cpp", "dimension": "2d", "root": "C:/repo/game",
        "source": "C:/repo/game/game.cpp", "frame": "C:/repo/game/frame.ppm",
    }
    monkeypatch.setattr(server.game_forge, "prepare_project", lambda *a, **k: project)
    monkeypatch.setattr(server.game_forge, "generation_prompt", lambda *a, **k: "prompt")

    def no_reference(*a, **k):
        raise ValueError("no reference")

    monkeypatch.setattr(server.game_forge, "reference_source", no_reference)
    monkeypatch.setattr(
        server, "sonder",
        lambda *a, **k: "```cpp\n#include <nlohmann/json.hpp>\nint main(){}\n```",
    )

    result = server._game_generate_result(
        "demo", "arena", "cpp", "2d", "arcane", 1, "code", 5, None,
        use_reference_fallback=False,
    )

    # None resolves to the cpp default of 2 repair rounds -> 3 attempts.
    assert len(result["attempts"]) == 3


def test_game_campaign_rotates_languages_and_dimensions(monkeypatch):
    seen = []

    def fake_result(name, concept, language, dimension, *args, **kwargs):
        seen.append((language, dimension))
        server.activity_tracker.record_model_call(
            model="fake-game-model", tokens_in=2, tokens_out=1,
        )
        return {
            "ok": True, "model_ok": True, "fallback_used": False,
            "name": name, "language": language, "dimension": dimension,
            "root": "C:/repo/" + name,
            "attempts": [{"attempt": 1, "ok": True, "output": "GAME_OK", "iid": "abc"}],
        }

    monkeypatch.setattr(server, "_game_generate_result", fake_result)

    server.activity_tracker.reset_for_tests()
    with server.activity_tracker.response_span("campaign", "four games") as activity:
        out = server.game_generation_campaign("fleet", total=4, max_workers=2)

    assert "4/4 runnable" in out
    assert set(seen) == {("python", "2d"), ("javascript", "2.5d"), ("cpp", "3d"), ("csharp", "2d")}
    assert activity["model_calls"] == 4
    assert activity["file_creates"] == 4
    assert activity["tool_calls"] == 1


def test_game_campaign_honors_explicit_language_and_dimension(monkeypatch):
    seen = []

    def fake_result(name, concept, language, dimension, *args, **kwargs):
        seen.append((language, dimension))
        return {
            "ok": True, "model_ok": True, "fallback_used": False,
            "name": name, "language": language, "dimension": dimension,
            "root": "C:/repo/" + name,
            "attempts": [
                {"attempt": 1, "ok": True, "output": "GAME_OK", "iid": "abc"}
            ],
        }

    monkeypatch.setattr(server, "_game_generate_result", fake_result)

    out = server.game_generation_campaign(
        "cpp-fleet", total=3, language="c++", dimension="isometric",
        max_workers=1,
    )

    assert "target=cpp/2.5d" in out
    assert seen == [("cpp", "2.5d")] * 3


def test_game_campaign_preserves_single_axis_constraints(monkeypatch):
    seen = []

    def fake_result(name, concept, language, dimension, *args, **kwargs):
        seen.append((language, dimension))
        return {
            "ok": True, "model_ok": True, "fallback_used": False,
            "name": name, "language": language, "dimension": dimension,
            "root": "C:/repo/" + name,
            "attempts": [{"attempt": 1, "ok": True, "output": "GAME_OK"}],
        }

    monkeypatch.setattr(server, "_game_generate_result", fake_result)

    server.game_generation_campaign(
        "cpp-dimensions", total=3, language="cpp", max_workers=1,
    )
    assert seen == [("cpp", "2d"), ("cpp", "2.5d"), ("cpp", "3d")]

    seen.clear()
    server.game_generation_campaign(
        "three-d-languages", total=4, dimension="3d", max_workers=1,
    )
    assert seen == [
        ("python", "3d"), ("javascript", "3d"),
        ("cpp", "3d"), ("csharp", "3d"),
    ]


def test_parallel_generate_run_uses_generated_code(monkeypatch):
    def fake_make_generate(*args, **kwargs):
        def gen(prompt, history=None):
            return "```python\nprint('candidate')\n```"
        return gen

    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    out = server.parallel_generate_run(
        "write a hello program",
        check="",
        variants=2,
        max_workers=2,
        timeout=8,
    )
    assert "parallel generate/run: 2/2 passed" in out
    assert "winner code:" in out
    assert "print('candidate')" in out


def test_parallel_generate_run_languages_spreads_languages(monkeypatch):
    def fake_make_generate(*args, **kwargs):
        def gen(prompt, history=None):
            if "javascript" in prompt:
                return "```javascript\nconsole.log('js')\n```"
            return "```python\nprint('py')\n```"
        return gen

    calls = []

    def fake_run_language_code(code, language, extra, timeout, execute=True):
        calls.append((language, code))
        return True, "%s ok" % language

    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    monkeypatch.setattr(server.grounding, "run_language_code", fake_run_language_code)
    out = server.parallel_generate_run_languages(
        "write tiny programs",
        languages="python,javascript",
        variants_per_language=1,
        max_workers=2,
    )
    assert "parallel multi-language generate/run: 2/2 passed" in out
    assert ("python", "print('py')") in calls
    assert ("javascript", "console.log('js')") in calls


def test_campaign_string_task_uses_the_actual_reversal():
    task = dict(server._CAMPAIGN_TASKS)["string"]
    assert "print exactly: rednos" in task
    assert server._campaign_expected("string") == "rednos"


def test_campaign_records_passing_interactions(monkeypatch):
    def fake_sonder(prompt, **kwargs):
        return "```python\nprint('sonder-ok')\n```\n\n[interaction_id: abc123]"

    records = []
    monkeypatch.setattr(server, "sonder", fake_sonder)
    monkeypatch.setattr(
        server.grounding,
        "run_language_code",
        lambda code, language, timeout=8, execute=True: (True, "sonder-ok"),
    )
    monkeypatch.setattr(server, "record_outcome", lambda iid, signal: records.append((iid, signal)) or "recorded")

    out = server.campaign_generate_compile_execute_record(
        total=1,
        languages="python",
        max_workers=1,
        repair_rounds=0,
    )
    assert "1/1 passed" in out
    assert records == [("abc123", "tests_passed")]


def test_campaign_repairs_then_records(monkeypatch):
    calls = {"n": 0}

    def fake_sonder(prompt, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "```python\nprint('wrong')\n```\n\n[interaction_id: bad123]"
        return "```python\nprint('sonder-ok')\n```\n\n[interaction_id: feed123]"

    outputs = iter([(True, "wrong"), (True, "sonder-ok")])
    records = []
    monkeypatch.setattr(server, "sonder", fake_sonder)
    monkeypatch.setattr(
        server.grounding,
        "run_language_code",
        lambda code, language, timeout=8, execute=True: next(outputs),
    )
    monkeypatch.setattr(server, "record_outcome", lambda iid, signal: records.append((iid, signal)) or "recorded")

    out = server.campaign_generate_compile_execute_record(
        total=1,
        languages="python",
        max_workers=1,
        repair_rounds=1,
    )
    assert "1/1 passed" in out
    assert records == [("feed123", "tests_passed")]


def test_campaign_records_terminal_failures(monkeypatch):
    def fake_sonder(prompt, **kwargs):
        return "```python\nprint('wrong')\n```\n\n[interaction_id: bad123]"

    records = []
    monkeypatch.setattr(server, "sonder", fake_sonder)
    monkeypatch.setattr(
        server.grounding,
        "run_language_code",
        lambda code, language, timeout=8, execute=True: (True, "wrong"),
    )
    monkeypatch.setattr(server, "record_outcome", lambda iid, signal: records.append((iid, signal)) or "recorded")

    out = server.campaign_generate_compile_execute_record(
        total=1,
        languages="python",
        max_workers=1,
        repair_rounds=0,
        record_failures=True,
    )
    assert "0/1 passed" in out
    assert "0 recorded, 1 failed-recorded" in out
    assert records == [("bad123", "failed")]


def test_learn_tiers_reports_all_defaults(monkeypatch):
    monkeypatch.delenv("SONDER_ALLOW_CLOUD", raising=False)
    out = server.learn_tiers()
    for tier in ("fast", "code", "general"):
        assert "%s: on" % tier in out
    for tier in ("cloud-code", "cloud-general"):
        assert "%s: disabled" % tier in out


def test_learn_tiers_distinguishes_available_cloud_from_learning(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    out = server.learn_tiers()

    assert "cloud-code: off" in out
    assert "cloud tiers are available" in out
    assert "cloud tiers require" not in out


def test_format_trace_contains_model_lessons_and_prompt():
    trace = {"lessons": ["prefer RRF", "avoid globals"], "augmented_prompt": "# Task:\nfix the bug"}
    params = {"temperature": 0.2, "num_predict": 1024, "num_ctx": 4096}
    out = server._format_trace("sonder", "code", params, trace)
    assert "sonder" in out
    assert "lessons retrieved: 2" in out
    assert "prefer RRF" in out
    assert "avoid globals" in out
    assert "# Task:\nfix the bug" in out


def test_format_trace_roundtrip_with_footer_does_not_break_id_parsing():
    trace = {"lessons": ["prefer RRF"], "augmented_prompt": "# Task:\nfix the bug"}
    params = {"temperature": 0.2, "num_predict": 1024, "num_ctx": 4096}
    trace_block = server._format_trace("sonder", "code", params, trace)
    # Mirrors the real tool's ordering: answer, then trace block, then footer LAST.
    body = server.with_footer("answer" + trace_block, "abcd1234")
    assert server.parse_interaction_id(body) == "abcd1234"
