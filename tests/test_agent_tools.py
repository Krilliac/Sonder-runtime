import pytest

import server


def test_host_receipt_uses_latest_validator_result(monkeypatch):
    responses = [
        '{"tool":"workspace_run","args":{"program":"python","args":["-m","pytest"]}}',
        '{"tool":"workspace_run","args":{"program":"python","args":["-m","pytest","tests"]}}',
        '{"final":"validation finished"}',
    ]
    observations = iter(["tests passed", "ERROR: broader suite failed"])
    monkeypatch.setattr(
        server, "_make_generate",
        lambda *args, **kwargs: lambda prompt, history=None: responses.pop(0),
    )
    monkeypatch.setattr(
        server, "_agent_dispatch_observed",
        lambda *args, **kwargs: next(observations),
    )

    receipt = server._agent_impl(
        "validate the workspace",
        max_steps=3,
        return_host_receipt=True,
    )

    assert receipt.validation_attempted
    assert not receipt.validation_passed


def test_extract_agent_json_accepts_plain_json():
    out = server._extract_agent_json('{"final": "done"}')
    assert out == {"final": "done"}


def test_extract_agent_json_accepts_wrapped_json():
    out = server._extract_agent_json('thinking...\n{"tool": "status", "args": {}}\n')
    assert out["tool"] == "status"


def test_agent_observation_prompt_bounds_context_and_keeps_recent_evidence():
    observations = [
        "step %d tool=file_read reason=inspect\nMARKER_%d\n%s"
        % (index, index, "x" * 2500)
        for index in range(1, 6)
    ]

    prompt = server._agent_observation_prompt(observations, max_chars=1800)

    assert len(prompt) <= 1800
    assert "Earlier observation summaries" in prompt
    assert "step 1 tool=file_read" in prompt
    assert "step 5 tool=file_read" in prompt
    assert "MARKER_5" in prompt
    assert "full host ledger retained" in prompt


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


def test_agent_dispatch_exposes_learning_health_as_read_only(monkeypatch):
    monkeypatch.setattr(
        server,
        "learning_health_status",
        lambda: "learning health: grounded",
    )

    assert server._agent_dispatch(
        "learning_health_status", {}, read_only=True
    ) == "learning health: grounded"


def test_embedding_mutations_require_learning_health_validation():
    mutations = [{
        "tool": "memory_interaction_embedding_backfill", "path": "",
    }]

    assert server._agent_validation_covers(
        "learning_health_status", {}, mutations,
    ) is True
    assert server._agent_validation_covers(
        "memory_quality_report", {}, mutations,
    ) is False


def test_agent_dispatch_can_tune_emotion_vectors(monkeypatch, tmp_path):
    monkeypatch.setattr(server.emotion_vectors, "workspace_root", lambda: str(tmp_path))
    monkeypatch.delenv("SONDER_EMOTION_VECTORS", raising=False)

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


def test_agent_repairs_invalid_json_decision_then_continues(monkeypatch):
    responses = [
        "I should inspect memory first.",
        '{"tool": "memory_search", "args": {"query": "adaptive"}}',
        '{"final": "done after repaired decision"}',
    ]
    prompts = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(
        server, "_agent_dispatch_observed", lambda *a, **k: "grounded observation",
    )

    output = server._agent_impl("inspect adaptive behavior", max_steps=2)

    assert output == "done after repaired decision"
    assert "HOST FORMAT REPAIR 1/2" in prompts[1]
    assert "exactly one JSON object" in prompts[1]
    assert "grounded observation" in prompts[2]


def test_agent_cancellation_stops_before_next_tool_dispatch(monkeypatch):
    responses = [
        '{"tool":"file_read","args":{"path":"README.md"}}',
        '{"tool":"file_write","args":{"path":"out.txt","content":"no"}}',
    ]
    cancelled = {"value": False}
    dispatches = []

    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *args, **kwargs: lambda prompt, history=None: responses.pop(0),
    )

    def dispatch(tool, *args, **kwargs):
        dispatches.append(tool)
        cancelled["value"] = True
        return "README evidence"

    monkeypatch.setattr(server, "_agent_dispatch_observed", dispatch)

    with pytest.raises(server.ModelCallError) as caught:
        server._agent_impl(
            "inspect then edit",
            max_steps=2,
            cancel_check=lambda: cancelled["value"],
        )

    assert caught.value.kind == "cancelled"
    assert dispatches == ["file_read"]


def test_agent_stops_repeating_identical_failed_tool_call(monkeypatch):
    responses = [
        '{"tool": "script_run", "args": {"path": "missing.py"}}',
        '{"tool": "script_run", "args": {"path": "missing.py"}}',
        '{"tool": "script_run", "args": {"path": "missing.py"}}',
        '{"tool": "script_run", "args": {"path": "missing.py"}}',
    ]
    prompts = []
    dispatches = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(
        server,
        "_agent_dispatch_observed",
        lambda *a, **k: dispatches.append(a) or "ERROR: missing.py",
    )

    output = server._agent_impl("run the script", max_steps=4)

    assert output.startswith("ERROR: agent repeated the same unsuccessful tool call 3 times")
    assert len(dispatches) == 2
    assert "HOST RECOVERY" in prompts[1]
    assert "HOST NO-PROGRESS" in prompts[3]


def test_agent_gets_final_only_pass_after_tool_step_budget(monkeypatch):
    responses = [
        '{"tool": "memory_search", "args": {"query": "one"}}',
        '{"tool": "memory_search", "args": {"query": "two"}}',
        '{"final": "synthesized after tool budget"}',
    ]
    prompts = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(
        server, "_agent_dispatch_observed", lambda *a, **k: "grounded evidence",
    )

    output = server._agent_impl(
        "inspect twice", max_steps=2, include_evidence=True,
    )

    assert output.startswith("synthesized after tool budget")
    assert "=== TOOL EVIDENCE ===" in output
    assert "HOST FINALIZATION ONLY" in prompts[2]


def test_negative_claim_review_repairs_schema(monkeypatch):
    responses = [
        "needs more evidence",
        '{"decision":"continue","reason":"query was paraphrased",'
        '"tool":"text_search","args":{"query":"Persistent autopilot"}}',
    ]
    prompts = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)

    review = server._agent_negative_claim_review(
        "Find the exact heading",
        "The heading was not found.",
        ["step 1 tool=text_search reason=find\n(no matches)"],
        "qwen-local",
    )

    assert review["decision"] == "continue"
    assert review["tool"] == "text_search"
    assert review["args"] == {"query": "Persistent autopilot"}
    assert "HOST SCHEMA ERROR" in prompts[1]


def test_negative_claim_review_requires_exact_named_heading(monkeypatch):
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("deterministic exact-anchor gate should run first")
        ),
    )

    review = server._agent_negative_claim_review(
        "Inspect README.md and report its Persistent autopilot heading.",
        "The README does not contain a Persistent autopilot heading.",
        [
            "step 1 tool=text_search reason=find\n"
            "text search: 'Persistent autopilot heading' under repo\n(no matches)"
        ],
        "qwen-local",
    )

    assert review == {
        "decision": "continue",
        "reason": "the exact task anchor 'Persistent autopilot' has not been searched",
        "tool": "text_search",
        "args": {
            "query": "Persistent autopilot",
            "root": ".",
            "regex": False,
            "max_results": 20,
            "glob": "README.md",
        },
    }


def test_agent_collects_more_evidence_after_negative_claim_review(monkeypatch):
    responses = [
        '{"tool":"file_read","args":{"path":"README.md"}}',
        '{"final":"The Persistent autopilot heading was not found."}',
        '{"tool":"text_search","args":{"query":"Persistent autopilot"}}',
        '{"final":"The Persistent autopilot heading is present."}',
    ]
    prompts = []
    reviews = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    def claim_review(*_args, **_kwargs):
        reviews.append(True)
        return {
            "decision": "continue",
            "reason": "the descriptive query did not prove the negative claim",
            "tool": "text_search",
            "args": {"query": "Persistent autopilot", "root": "."},
        }

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(server, "_agent_negative_claim_review", claim_review)
    monkeypatch.setattr(
        server,
        "_agent_dispatch_observed",
        lambda tool, *_args, **_kwargs: (
            "### Persistent autopilot" if tool == "text_search" else "README excerpt"
        ),
    )

    output = server._agent_impl("Find the Persistent autopilot heading", max_steps=4)

    assert output == "The Persistent autopilot heading is present."
    assert len(reviews) == 1
    assert "HOST CLAIM REVIEW" in prompts[2]
    assert "### Persistent autopilot" in prompts[2]


def test_agent_bounds_repeated_negative_claim_recovery(monkeypatch):
    responses = [
        '{"tool":"file_read","args":{"path":"README.md"}}',
        '{"final":"The heading was not found."}',
        '{"final":"The heading was not found."}',
        '{"final":"The heading was not found."}',
    ]
    monkeypatch.setattr(
        server,
        "_make_generate",
        lambda *a, **k: lambda prompt, history=None: responses.pop(0),
    )
    monkeypatch.setattr(
        server, "_agent_dispatch_observed", lambda *_args, **_kwargs: "README excerpt",
    )
    monkeypatch.setattr(
        server,
        "_agent_negative_claim_review",
        lambda *_args, **_kwargs: {
            "decision": "continue",
            "reason": "the exact anchor was never searched",
            "tool": "text_search",
            "args": {"query": "exact heading", "root": "."},
        },
    )

    output = server._agent_impl("Find a heading", max_steps=2)

    assert output.startswith("EVIDENCE_REQUIRED")
    assert "exact anchor was never searched" in output


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


def test_agent_required_web_fetch_rejects_empty_page_before_memory_final(
    monkeypatch,
):
    responses = [
        '{"tool": "web_fetch", "args": {"url": "https://example.com"}}',
        '{"final": "Python 3.10.6, from memory."}',
    ]
    prompts = []

    def generate(prompt, history=None):
        prompts.append(prompt)
        return responses.pop(0)

    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: generate)
    monkeypatch.setattr(
        server, "_agent_dispatch_observed", lambda *a, **k: "  \n\t",
    )

    output = server._agent_impl(
        "What is the latest Python version?",
        max_steps=2,
        required_tool_names=("web_fetch",),
    )

    assert output.startswith("ERROR: agent reached max_steps=2")
    assert "web_fetch" in output
    assert "Python 3.10.6" not in output
    assert "HOST RECOVERY" in prompts[1]


def test_agent_tool_evidence_keeps_zero_output_valid_for_non_web_tools():
    assert server._agent_tool_observation_ok("workspace_run", "0") is True
    assert server._agent_tool_observation_ok("web_fetch", "0") is True
    assert server._agent_tool_observation_ok("web_fetch", " \n\t") is False
    assert server._agent_tool_observation_ok("web_fetch", None) is False


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
