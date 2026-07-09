import memory_store
import server


def test_with_footer_and_parse_roundtrip():
    out = server.with_footer("here is code", "abc123def4567890")
    assert out.endswith("[interaction_id: abc123def4567890]")
    assert server.parse_interaction_id(out) == "abc123def4567890"


def test_parse_none_when_absent():
    assert server.parse_interaction_id("just some text") is None


def test_resolve_trilobite_falls_back(monkeypatch):
    # no alias present -> code tier model
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    assert server.resolve_trilobite_model() == server.TIERS["code"]


def test_resolve_trilobite_prefers_alias(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "trilobite:latest"}]})
    assert server.resolve_trilobite_model() == "trilobite"


def test_resolve_trilobite_soft_fails_when_ollama_down(monkeypatch):
    def boom(path):
        raise Exception("ollama down")
    monkeypatch.setattr(server, "_get", boom)
    assert server.resolve_trilobite_model() == server.TIERS["code"]


def test_resolve_trilobite_strict_true_prefers_alias(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "trilobite:latest"}]})
    assert server.resolve_trilobite_model(strict=True) == "trilobite"


def test_resolve_trilobite_strict_true_alias_absent_returns_none(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    assert server.resolve_trilobite_model(strict=True) is None


def test_resolve_trilobite_strict_false_alias_absent_falls_back(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})
    assert server.resolve_trilobite_model(strict=False) == server.TIERS["code"]


def test_trilobite_strict_true_errors_when_alias_missing_before_any_ollama_call(monkeypatch):
    monkeypatch.setattr(server, "_get", lambda path: {"models": [{"name": "qwen2.5:3b"}]})

    def boom_post(path, payload):
        raise AssertionError("must not call Ollama when strict + alias missing")
    monkeypatch.setattr(server, "_post", boom_post)

    out = server.trilobite("hi", strict=True)
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

    monkeypatch.setenv("LOCAL_LLM_NUM_THREAD", "12")
    monkeypatch.setenv("LOCAL_LLM_NUM_GPU", "99")
    monkeypatch.setenv("LOCAL_LLM_NUM_BATCH", "256")
    monkeypatch.setattr(server, "_post", fake_post)

    gen = server._make_generate("local-model", "system", 0.3, 77, 4096)
    assert gen("hello") == "ok"
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
    monkeypatch.setenv("TRILOBITE_NATIVE_CONTEXT_MAX", "256k")

    opts = server._local_model_options(0.2, 10, 1000000)

    assert opts["num_ctx"] == 256000


def test_make_generate_cloud_omits_local_runtime_options(monkeypatch):
    seen = {}

    def fake_post(path, payload):
        seen["payload"] = payload
        return {"message": {"content": "ok"}}

    monkeypatch.setenv("LOCAL_LLM_NUM_THREAD", "12")
    monkeypatch.setenv("LOCAL_LLM_NUM_GPU", "99")
    monkeypatch.setenv("LOCAL_LLM_NUM_BATCH", "256")
    monkeypatch.setattr(server, "_post", fake_post)

    gen = server._make_generate("cloud-model", "", 0.4, 88, 8192, cloud=True)
    assert gen("hello") == "ok"
    assert "keep_alive" not in seen["payload"]
    assert seen["payload"]["options"] == {"temperature": 0.4, "num_predict": 88}


def test_serve_target_cloud_tier_requires_opt_in(monkeypatch):
    monkeypatch.delenv("TRILOBITE_ALLOW_CLOUD", raising=False)
    model, cloud, augment, label = server._serve_target("cloud-code", None)
    assert model is None
    assert cloud is True
    assert augment is False
    assert label == "cloud-disabled"


def test_serve_target_cloud_tier_is_clean_teacher_when_enabled(monkeypatch):
    monkeypatch.setenv("TRILOBITE_ALLOW_CLOUD", "1")
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
    monkeypatch.setenv("TRILOBITE_ALLOW_CLOUD", "1")
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
    assert server._canonical_learn_tier("trilobite") == "code"
    assert server._canonical_learn_tier("cloud-code") == "cloud-code"
    assert server._canonical_learn_tier("general") == "general"


def test_trilobite_tool_unknown_tier_errors_before_ollama(monkeypatch):
    def boom_post(path, payload):
        raise AssertionError("must not call Ollama for an unknown tier")
    monkeypatch.setattr(server, "_post", boom_post)
    out = server.trilobite("hi", tier="does-not-exist")
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
    for name in ("", "trilobite", "local", None):
        model, cloud, augment, label = server._serve_target(name, None)
        assert model == server.TIERS["code"]  # falls back to base coder alias target
        assert cloud is False
        assert augment is True
        assert label == "trilobite"


def test_trilobite_stats_runs_against_empty_db(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "empty.db"))
    out = server.trilobite_stats()
    assert isinstance(out, str)
    assert "lessons:" in out


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
    assert "trilobite context health" in out
    assert "context [" in out
    assert "native" in out
    assert "memory  [" in out


def test_set_context_size_selects_virtual_context(monkeypatch):
    monkeypatch.setenv("TRILOBITE_NATIVE_CONTEXT_MAX", "256k")
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


def test_trilobite_slash_command_does_not_call_model(monkeypatch):
    monkeypatch.setattr(server, "context_health", lambda: "context health")
    monkeypatch.setattr(server, "_serve_target", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("model should not resolve")))

    assert server.trilobite("/context") == "context health"


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
    assert "trilobite improvement report" in text
    assert "next improvements:" in text


def test_improvement_report_honors_cloud_opt_in(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setenv("TRILOBITE_ALLOW_CLOUD", "1")

    report = server.improvement_report_data()

    assert report["cloud_allowed"] is True
    assert not any(i["area"] == "deployment" for i in report["issues"])


def test_master_orchestrate_asks_for_execution_mode():
    out = server.master_orchestrate("build a parser", mode="ask", agents=2)

    assert "Choose execution mode" in out
    assert "inline" in out
    assert "delegate" in out


def test_master_orchestrate_delegates_and_audits(monkeypatch):
    calls = []

    def fake_offload(prompt, **kwargs):
        calls.append(prompt)
        if "Audit these delegated outputs" in prompt or "master orchestrator" in prompt.lower():
            return "audited merge"
        return "agent output"

    monkeypatch.setattr(server, "offload", fake_offload)

    out = server.master_orchestrate("find risks", mode="delegate", agents=2)

    assert "master orchestration complete" in out
    assert "audited merge" in out
    assert len(calls) == 3
    assert "active agents: 0" in server.master_status()


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
    monkeypatch.setenv("TRILOBITE_FILE_APPROVAL_CODE", "let-me")

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


def test_campaign_records_passing_interactions(monkeypatch):
    def fake_trilobite(prompt, **kwargs):
        return "```python\nprint('trilobite-ok')\n```\n\n[interaction_id: abc123]"

    records = []
    monkeypatch.setattr(server, "trilobite", fake_trilobite)
    monkeypatch.setattr(
        server.grounding,
        "run_language_code",
        lambda code, language, timeout=8, execute=True: (True, "trilobite-ok"),
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

    def fake_trilobite(prompt, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            return "```python\nprint('wrong')\n```\n\n[interaction_id: bad123]"
        return "```python\nprint('trilobite-ok')\n```\n\n[interaction_id: feed123]"

    outputs = iter([(True, "wrong"), (True, "trilobite-ok")])
    records = []
    monkeypatch.setattr(server, "trilobite", fake_trilobite)
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
    def fake_trilobite(prompt, **kwargs):
        return "```python\nprint('wrong')\n```\n\n[interaction_id: bad123]"

    records = []
    monkeypatch.setattr(server, "trilobite", fake_trilobite)
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
    monkeypatch.delenv("TRILOBITE_ALLOW_CLOUD", raising=False)
    out = server.learn_tiers()
    for tier in ("fast", "code", "general"):
        assert "%s: on" % tier in out
    for tier in ("cloud-code", "cloud-general"):
        assert "%s: disabled" % tier in out


def test_format_trace_contains_model_lessons_and_prompt():
    trace = {"lessons": ["prefer RRF", "avoid globals"], "augmented_prompt": "# Task:\nfix the bug"}
    params = {"temperature": 0.2, "num_predict": 1024, "num_ctx": 4096}
    out = server._format_trace("trilobite", "code", params, trace)
    assert "trilobite" in out
    assert "lessons retrieved: 2" in out
    assert "prefer RRF" in out
    assert "avoid globals" in out
    assert "# Task:\nfix the bug" in out


def test_format_trace_roundtrip_with_footer_does_not_break_id_parsing():
    trace = {"lessons": ["prefer RRF"], "augmented_prompt": "# Task:\nfix the bug"}
    params = {"temperature": 0.2, "num_predict": 1024, "num_ctx": 4096}
    trace_block = server._format_trace("trilobite", "code", params, trace)
    # Mirrors the real tool's ordering: answer, then trace block, then footer LAST.
    body = server.with_footer("answer" + trace_block, "abcd1234")
    assert server.parse_interaction_id(body) == "abcd1234"
