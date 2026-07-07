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


def test_should_learn_includes_code_and_cloud_tiers():
    # Default LEARN_TIERS = code + both cloud tiers (teacher distillation).
    assert server._should_learn("code", True) is True
    assert server._should_learn("cloud-code", True) is True
    assert server._should_learn("cloud-general", True) is True
    # Mechanical tiers and learn=False never learn.
    assert server._should_learn("fast", True) is False
    assert server._should_learn("general", True) is False
    assert server._should_learn("code", False) is False
    assert server._should_learn("cloud-code", False) is False


def test_should_learn_honors_learn_tiers(monkeypatch):
    monkeypatch.setattr(server, "LEARN_TIERS", {"code"})
    assert server._should_learn("code", True) is True
    assert server._should_learn("cloud-code", True) is False


def test_serve_target_cloud_tier_is_clean_teacher():
    # Cloud tier: real cloud model, cloud=True, augment=False (clean), labeled by tier.
    model, cloud, augment, label = server._serve_target("cloud-code", None)
    assert model == server.TIERS["cloud-code"]
    assert cloud is True
    assert augment is False
    assert label == "cloud-code"


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
