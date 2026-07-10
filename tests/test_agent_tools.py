import server


def test_extract_agent_json_accepts_plain_json():
    out = server._extract_agent_json('{"final": "done"}')
    assert out == {"final": "done"}


def test_extract_agent_json_accepts_wrapped_json():
    out = server._extract_agent_json('thinking...\n{"tool": "status", "args": {}}\n')
    assert out["tool"] == "status"


def test_agent_dispatch_blocks_web_when_disabled():
    out = server._agent_dispatch("web_search", {"query": "x"}, allow_web=False)
    assert out.startswith("ERROR: web access disabled")


def test_agent_dispatch_can_tune_emotion_vectors(monkeypatch, tmp_path):
    monkeypatch.setattr(server.emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("TRILOBITE_EMOTION_VECTORS", raising=False)

    out = server._agent_dispatch(
        "tune_emotion_vectors",
        {"feedback_text": "be warmer and more concise"},
    )

    assert "Tuned emotion vectors" in out
    vectors = server.emotion_vectors.read_vectors()
    assert vectors["warmth"] > server.emotion_vectors.DEFAULT_VECTORS["warmth"]
    assert vectors["brevity"] > server.emotion_vectors.DEFAULT_VECTORS["brevity"]


def test_agent_dispatch_can_learn_preference(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "prefs.db"))

    out = server._agent_dispatch(
        "learn_preference",
        {"text": "User prefers direct answers."},
    )

    assert "Learned preference" in out
    assert "User prefers direct answers." in server.preferences_status()


def test_agent_runs_tool_then_final(monkeypatch):
    responses = [
        '{"tool": "memory_search", "args": {"query": "deque"}, "reason": "check memory"}',
        '{"final": "done after observation"}',
    ]
    prompts = []

    def fake_make_generate(*args, **kwargs):
        def gen(prompt, history=None):
            prompts.append(prompt)
            return responses.pop(0)
        return gen

    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    monkeypatch.setattr(server, "_agent_dispatch", lambda tool, args, allow_web=True: "OBSERVATION")
    out = server.agent("answer with tools", tier="code", max_steps=2)
    assert out.startswith("done after observation")
    assert "=== ACTIVITY (observable work) ===" in out
    assert "tool calls:" in out
    assert "OBSERVATION" in prompts[1]


def test_agent_reports_parse_error(monkeypatch):
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: lambda prompt, history=None: "not json")
    out = server.agent("x", tier="code", max_steps=1)
    assert out.startswith("ERROR: could not parse agent decision")


def test_agent_requires_successful_file_evidence(monkeypatch):
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *a, **k: lambda prompt, history=None: '{"final": "I inspected it and it is correct."}',
    )

    out = server._agent_impl(
        "Review Repository: D:\\SparkEngine",
        tier="code",
        max_steps=1,
        require_file_evidence=True,
        read_only=True,
        include_evidence=True,
    )

    assert out.startswith("EVIDENCE_REQUIRED:")
    assert "I inspected it" not in out


def test_agent_attaches_successful_file_evidence(monkeypatch):
    responses = [
        '{"tool": "file_read", "args": {"path": "README.md"}, "reason": "inspect source"}',
        '{"final": "README says hello."}',
    ]
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *a, **k: lambda prompt, history=None: responses.pop(0),
    )
    monkeypatch.setattr(
        server,
        "_agent_dispatch_observed",
        lambda tool, args, allow_web=True: "file read: README.md\nhello",
    )

    out = server._agent_impl(
        "Review Repository: local",
        tier="code",
        max_steps=2,
        require_file_evidence=True,
        read_only=True,
        include_evidence=True,
    )

    assert out.startswith("README says hello.")
    assert "=== TOOL EVIDENCE ===" in out
    assert "tool=file_read" in out
