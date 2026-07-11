import server


def test_extract_agent_json_accepts_plain_json():
    out = server._extract_agent_json('{"final": "done"}')
    assert out == {"final": "done"}


def test_extract_agent_json_accepts_wrapped_json():
    out = server._extract_agent_json('thinking...\n{"tool": "status", "args": {}}\n')
    assert out["tool"] == "status"


def test_agent_dispatch_blocks_web_when_disabled():
    for tool, args in (
        ("web_search", {"query": "x"}),
        ("web_fetch", {"url": "https://example.com"}),
        ("weather_lookup", {"location": "Chicago"}),
        ("approximate_location_lookup", {"consent": True}),
    ):
        out = server._agent_dispatch(tool, args, allow_web=False)
        assert out.startswith("ERROR: web access disabled")


def test_agent_dispatch_requires_host_verified_location_consent(monkeypatch):
    monkeypatch.setattr(
        server, "approximate_location_lookup",
        lambda consent=False: "Approximate location: Chicago" if consent else "ERROR",
    )

    denied = server._agent_dispatch(
        "approximate_location_lookup", {"consent": True}, allow_web=True,
    )
    allowed = server._agent_dispatch(
        "approximate_location_lookup", {"consent": True}, allow_web=True,
        allow_location=True,
    )

    assert "host-verified user consent" in denied
    assert allowed == "Approximate location: Chicago"


def test_agent_dispatch_routes_fleet_capacity_and_cancellation(monkeypatch):
    monkeypatch.setattr(
        server, "master_capacity", lambda requested_agents=0: f"capacity:{requested_agents}",
    )
    monkeypatch.setattr(
        server, "master_cancel", lambda agent_id: f"cancel:{agent_id}",
    )
    monkeypatch.setattr(
        server, "master_retry", lambda agent_id, tier="": f"retry:{agent_id}:{tier}",
    )

    assert server._agent_dispatch(
        "master_capacity", {"requested_agents": 12}, read_only=True,
    ) == "capacity:12"
    assert server._agent_dispatch(
        "master_cancel", {"agent_id": "master-abc"}, read_only=False,
    ) == "cancel:master-abc"
    assert server._agent_dispatch(
        "master_retry", {"agent_id": "master-old", "tier": "code"},
        read_only=False,
    ) == "retry:master-old:code"
    denied = server._agent_dispatch(
        "master_cancel", {"agent_id": "all"}, read_only=True,
    )
    assert denied.startswith("ERROR:")
    assert "not allowed" in denied
    retry_denied = server._agent_dispatch(
        "master_retry", {"agent_id": "master-old"}, read_only=True,
    )
    assert "not allowed" in retry_denied


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
    monkeypatch.setattr(
        server,
        "_agent_dispatch",
        lambda tool, args, allow_web=True, read_only=False: "OBSERVATION",
    )
    out = server.agent("answer with tools", tier="code", max_steps=2, checklist=False)
    assert out.startswith("done after observation")
    assert "=== ACTIVITY (observable work) ===" in out
    assert "tool calls:" in out
    assert "OBSERVATION" in prompts[1]


def test_agent_reports_parse_error(monkeypatch):
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: lambda prompt, history=None: "not json")
    out = server.agent("x", tier="code", max_steps=1)
    assert out.startswith("ERROR: could not parse agent decision")


def test_agent_host_requires_successful_web_tool_before_final(monkeypatch):
    responses = [
        '{"final": "I cannot access the web."}',
        '{"tool": "web_search", "args": {"query": "current news"}}',
        '{"final": "Here are the current results."}',
    ]
    prompts = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(
        server,
        "_agent_dispatch_observed",
        lambda *a, **k: "1. Current result\n   https://example.com",
    )

    output = server._agent_impl(
        "Find current news",
        max_steps=3,
        required_tool_names=("web_search", "web_fetch"),
    )

    assert output == "Here are the current results."
    assert "HOST REQUIREMENT" in prompts[1]


def test_agent_rejects_final_when_required_web_tool_never_runs(monkeypatch):
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *a, **k: lambda prompt, history=None: '{"final": "No tools."}',
    )

    output = server._agent_impl(
        "Find current news", max_steps=1,
        required_tool_names=("web_search",),
    )

    assert output.startswith("ERROR: agent reached max_steps=1")
    assert "required web tool" in output


def test_agent_does_not_repeat_identical_successful_web_call(monkeypatch):
    responses = [
        '{"tool": "web_fetch", "args": {"url": "https://example.com"}}',
        '{"tool": "web_fetch", "args": {"url": "https://example.com"}}',
        '{"final": "Used the fetched page."}',
    ]
    dispatches = []

    monkeypatch.setattr(
        server, "_make_generate",
        lambda *args, **kwargs: lambda prompt, history=None: responses.pop(0),
    )

    def fake_dispatch(tool, args, **kwargs):
        dispatches.append((tool, args))
        return "fetched page"

    monkeypatch.setattr(server, "_agent_dispatch_observed", fake_dispatch)

    output = server._agent_impl(
        "Fetch the page", max_steps=3, required_tool_names=("web_fetch",),
    )

    assert output == "Used the fetched page."
    assert len(dispatches) == 1


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
        lambda tool, args, allow_web=True, read_only=False: "file read: README.md\nhello",
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
