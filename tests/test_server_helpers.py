import importlib
import json
import threading
import time

import memory_store
import pytest
import server


def _host_repo_result(project, output="grounded result"):
    return server.autopilot_controller.HostTaskResult(
        output=(
            output
            + "\n\n=== TOOL EVIDENCE ===\nstep 1 tool=file_read\nscoped source"
        ),
        tools=("file_read",),
        project_scope=str(project),
    )


def setup_function():
    server.master_orchestrator.reset_for_tests()


def test_with_footer_and_parse_roundtrip():
    out = server.with_footer("here is code", "abc123def4567890")
    assert out.endswith("[interaction_id: abc123def4567890]")
    assert server.parse_interaction_id(out) == "abc123def4567890"


def test_parse_none_when_absent():
    assert server.parse_interaction_id("just some text") is None


def test_master_orchestrate_never_forges_a_repo_scoped_task(monkeypatch, tmp_path):
    # Regression for a 2026-07-13 bug: master_orchestrate() called
    # creative_router.classify(task) BEFORE checking
    # master_orchestrator.requires_repository_tools(task), so an ordinary
    # code-analysis task containing a common verb+noun pair the creative
    # regex also matches (e.g. "generate" + "model"/"document"/"diagram")
    # was misrouted into the greenfield game/artifact forge pipeline instead
    # of being handled as repo-scoped work -- it has no filesystem access,
    # so it silently returned unrelated fake game-build output.
    #
    # requires_repository_tools is a real regex match (not mocked) so this
    # exercises the actual production classification, not a stub.
    forge_calls = []
    monkeypatch.setattr(
        server, "_master_grounded_build",
        lambda *a, **k: forge_calls.append((a, k)) or "SHOULD NOT BE CALLED",
    )
    monkeypatch.setattr(
        server.master_orchestrator, "run_inline",
        lambda task, worker, **kwargs: {"output": "stub-inline-result", "master_id": "m-test"},
    )

    task = (
        "Read the current repository files and generate a structural model "
        "of the class groupings, then draft a one-line document summary."
    )
    assert server.master_orchestrator.requires_repository_tools(task), (
        "test task must actually match the repository-tools detector, "
        "otherwise this test doesn't exercise the guard it's regressing"
    )

    result = server.master_orchestrate(
        task=task, mode="inline", project=str(tmp_path),
    )

    assert forge_calls == [], (
        "master_orchestrate routed a repository-scoped task into the "
        "greenfield forge pipeline -- the requires_repository_tools guard "
        "must be checked before creative_router.classify()"
    )
    assert result == "stub-inline-result"


def test_master_orchestrate_still_forges_explicit_greenfield_requests(monkeypatch):
    # The fix above must not break genuine greenfield build requests that
    # don't reference existing repository state.
    forge_calls = []
    monkeypatch.setattr(
        server, "_master_grounded_build",
        lambda task, mode, tier, intent, retry_of="": forge_calls.append(intent) or "forged",
    )

    task = "Create a C++ 2.5D isometric Diablo-like RPG game with in-house assets and no third-party libraries."
    assert not server.master_orchestrator.requires_repository_tools(task)

    result = server.master_orchestrate(task=task, mode="inline")

    assert len(forge_calls) == 1
    assert forge_calls[0]["kind"] in ("game", "game_campaign")
    assert result == "forged"


def test_master_text_only_threat_model_stays_in_requested_fleet(monkeypatch):
    forge_calls = []
    fleet_calls = []
    monkeypatch.setattr(
        server, "_master_grounded_build",
        lambda *args, **kwargs: forge_calls.append((args, kwargs)) or "forged",
    )

    def start_fleet(task, **kwargs):
        fleet_calls.append((task, kwargs))
        return {
            "master_id": "master-text-review",
            "agents": ["agent-1", "agent-2"],
            "worker_slots": 2,
        }

    monkeypatch.setattr(
        server.master_orchestrator, "start_delegated", start_fleet,
    )
    task = (
        "Greenfield operating-system IPC design exercise. No files are needed. "
        "Independently threat-model a service directory and duplex ChannelCore. "
        "Generate minimal P0 race counterexamples and deterministic runtime tests; "
        "avoid implementation code."
    )

    result = server.master_orchestrate(
        task=task, mode="fleet", agents=2, tier="cloud-code",
    )

    assert forge_calls == []
    assert len(fleet_calls) == 1
    assert fleet_calls[0][0] == task
    assert "mode: fleet | master=master-text-review | agents=2" in result


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


def test_resolve_sonder_missing_alias_returns_stable_setup_target(monkeypatch):
    monkeypatch.setitem(server.TIERS, "code", server.SONDER_STABLE_ALIAS)
    monkeypatch.setattr(
        server,
        "_get",
        lambda path: {"models": [{"name": "sonder:experimental"}]},
    )

    assert server.resolve_sonder_model(strict=False) == server.LOCAL_CODE_MODEL
    assert server.resolve_sonder_model(strict=False) == server.TIERS["code"]


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


def test_local_model_options_leave_accelerator_placement_to_ollama(monkeypatch):
    monkeypatch.delenv("SONDER_NUM_GPU", raising=False)

    opts = server._local_model_options(0.2, 10, 4096)

    assert "num_gpu" not in opts


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
    assert "think" not in seen["payload"]


@pytest.mark.parametrize(
    ("model", "expected_think"),
    [
        ("kimi-k3:cloud", True),
        ("KIMI-K3:preview-cloud", True),
        ("glm-5.2:cloud", "high"),
        ("GLM-5.2:preview-cloud", "high"),
        ("kimi-k2.7-code:cloud", True),
        ("KIMI-K2.7-CODE:preview-cloud", True),
        ("gpt-oss:120b-cloud", "low"),
        ("GPT-OSS:custom-cloud", "low"),
        ("custom-reasoner:cloud", None),
    ],
)
def test_make_generate_applies_known_cloud_thinking_policy(
    monkeypatch, model, expected_think,
):
    seen = {}

    def fake_post(path, payload, timeout=None):
        seen["payload"] = payload
        return {"message": {"content": "ok"}}

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(server, "_post", fake_post)

    gen = server._make_generate(model, "", 0.4, 88, 8192, cloud=True)
    assert gen("hello") == "ok"
    if expected_think is None:
        assert "think" not in seen["payload"]
    else:
        assert seen["payload"]["think"] == expected_think
    if model.casefold().startswith(("kimi-k3:", "kimi-k2.7-code:", "glm-5.2:")):
        assert seen["payload"]["options"]["num_predict"] == 4096


@pytest.mark.parametrize(
    ("model", "expected_think"),
    [
        ("kimi-k3:cloud", True),
        ("glm-5.2:cloud", "high"),
        ("kimi-k2.7-code:cloud", True),
        ("gpt-oss:120b-cloud", "low"),
        ("custom-reasoner:cloud", None),
    ],
)
def test_non_learning_offload_applies_known_cloud_thinking_policy(
    monkeypatch, model, expected_think,
):
    seen = {}

    def fake_post(path, payload, timeout=None):
        seen["payload"] = payload
        return {"message": {"content": "ok"}}

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setitem(server.TIERS, "cloud-code", model)
    monkeypatch.setattr(server, "_post", fake_post)

    assert server._offload_impl("hello", tier="cloud-code", learn=False) == "ok"
    if expected_think is None:
        assert "think" not in seen["payload"]
    else:
        assert seen["payload"]["think"] == expected_think
    if model.casefold().startswith(("kimi-k3:", "kimi-k2.7-code:", "glm-5.2:")):
        assert seen["payload"]["options"]["num_predict"] == 4096


def test_thinking_only_response_reports_sanitized_metadata_without_reasoning(
    monkeypatch,
):
    secret_thinking = "private chain of thought: bearer super-secret-token"
    calls = []

    def fake_post(path, payload):
        calls.append(payload)
        return {
            "message": {
                "content": "",
                "thinking": secret_thinking,
                "tool_calls": [{"function": {"name": "one"}}, {"secret": "two"}],
            },
            "eval_count": 19,
            "done_reason": "stop\nunsafe suffix!",
        }

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(server, "_post", fake_post)

    with pytest.raises(server.ModelCallError) as captured:
        server._chat_request(
            {"model": "gpt-oss:120b-cloud", "messages": [], "stream": False},
            model="gpt-oss:120b-cloud",
            cloud=True,
        )

    error = captured.value
    prefix, raw_metadata = error.detail.split("; metadata=", 1)
    assert prefix == "Ollama returned no assistant content"
    assert json.loads(raw_metadata) == {
        "done_reason": "other",
        "eval_count": 19,
        "thinking_chars": len(secret_thinking),
        "tool_call_count": 2,
    }
    assert secret_thinking not in error.detail
    assert "super-secret-token" not in server._format_model_call_error(error)
    assert "unsafe suffix" not in error.detail
    assert error.attempts == 1
    assert len(calls) == 1


@pytest.mark.parametrize(
    "arguments",
    [
        {"path": "README.md", "start_line": 1},
        '{"path":"README.md","start_line":1}',
    ],
)
def test_agent_chat_accepts_one_native_tool_call_without_exposing_thinking(
    monkeypatch, arguments,
):
    secret_thinking = "private chain of thought: bearer hidden-token"
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")

    def fake_post(path, payload):
        return {
            "message": {
                "content": "",
                "thinking": secret_thinking,
                "tool_calls": [{
                    "function": {
                        "name": "file_read_range",
                        "arguments": arguments,
                    },
                }],
            },
        }

    monkeypatch.setattr(server, "_post", fake_post)

    _, content = server._chat_request(
        {"model": "hosted", "messages": [], "stream": False},
        model="hosted",
        cloud=True,
        accept_native_tool_calls=True,
    )

    assert json.loads(content) == {
        "tool": "file_read_range",
        "args": {"path": "README.md", "start_line": 1},
        "reason": "model native tool call",
    }
    assert secret_thinking not in content
    assert "hidden-token" not in content


def test_normal_chat_still_rejects_single_native_tool_call(monkeypatch):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(
        server,
        "_post",
        lambda *args, **kwargs: {
            "message": {
                "content": "",
                "tool_calls": [{
                    "function": {"name": "status", "arguments": {}},
                }],
            },
        },
    )

    with pytest.raises(server.ModelCallError, match="no assistant content"):
        server._chat_request({}, model="hosted", cloud=True)


@pytest.mark.parametrize(
    "tool_calls",
    [
        [
            {"function": {"name": "one", "arguments": {}}},
            {"function": {"name": "two", "arguments": {}}},
        ],
        [{"function": {"name": "file_read", "arguments": "not json"}}],
        [{"function": {"name": "file_read", "arguments": ["README.md"]}}],
        [{"function": {"name": "bad tool name", "arguments": {}}}],
        [{"function": {"name": "file_read"}}],
    ],
)
def test_agent_chat_rejects_noncanonical_native_tool_calls(monkeypatch, tool_calls):
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(
        server,
        "_post",
        lambda *args, **kwargs: {
            "message": {"content": "", "tool_calls": tool_calls},
        },
    )

    with pytest.raises(server.ModelCallError, match="no assistant content"):
        server._chat_request(
            {},
            model="hosted",
            cloud=True,
            accept_native_tool_calls=True,
        )


def test_agent_chat_rejects_recursively_nested_native_arguments(monkeypatch):
    deeply_nested = '{"a":' * 10000 + "{}" + "}" * 10000
    assert len(deeply_nested) < server._NATIVE_TOOL_ARGUMENTS_MAX_CHARS
    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(
        server,
        "_post",
        lambda *args, **kwargs: {
            "message": {
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "file_read",
                        "arguments": deeply_nested,
                    },
                }],
            },
        },
    )

    with pytest.raises(server.ModelCallError, match="no assistant content"):
        server._chat_request(
            {},
            model="hosted",
            cloud=True,
            accept_native_tool_calls=True,
        )


@pytest.mark.parametrize(
    ("model", "expected_think"),
    [
        ("kimi-k2.7-code:cloud", False),
        ("glm-5.2:cloud", False),
    ],
)
def test_agent_generate_uses_compact_cloud_reasoning_and_native_tools(
    monkeypatch, model, expected_think,
):
    seen = {}

    def fake_post(path, payload, timeout=None):
        seen["payload"] = payload
        return {
            "message": {
                "content": "",
                "tool_calls": [{
                    "function": {
                        "name": "status",
                        "arguments": "{}",
                    },
                }],
            },
        }

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(server, "_post", fake_post)

    gen = server._make_generate(
        model,
        "",
        0.1,
        1200,
        8192,
        cloud=True,
        accept_native_tool_calls=True,
        compact_cloud_reasoning=True,
    )
    content = gen("choose a tool")

    assert json.loads(content)["tool"] == "status"
    assert seen["payload"]["think"] == expected_think
    assert seen["payload"]["options"]["num_predict"] == 1200


def test_make_generate_honors_bounded_prediction_override(monkeypatch):
    seen = {}

    def fake_post(path, payload, timeout=None):
        seen["payload"] = payload
        return {"message": {"content": "ok"}, "eval_count": 2}

    monkeypatch.setenv("SONDER_ALLOW_CLOUD", "1")
    monkeypatch.setattr(server, "_post", fake_post)
    gen = server._make_generate(
        "glm-5.2:cloud", "", 0.1, 1200, 8192, cloud=True,
        compact_cloud_reasoning=True,
    )
    gen.num_predict_override = 37

    assert gen("bounded") == "ok"
    assert seen["payload"]["options"]["num_predict"] == 37


def test_bounded_cloud_agent_generate_caps_aggregate_output():
    limits = []

    def raw(_prompt, history=None):
        limit = raw.num_predict_override
        limits.append(limit)
        raw.last_usage = {"tokens_out": limit}
        raw.last_response_meta = {"done_reason": "stop"}
        return "ok"

    raw.num_predict_override = None
    raw.last_usage = {}
    raw.last_response_meta = {}
    gen = server._bounded_cloud_agent_generate(
        raw, per_call_limit=3, total_budget=5,
    )

    assert gen("one") == "ok"
    assert gen("two") == "ok"
    with pytest.raises(server.ModelCallError) as caught:
        gen("three")

    assert limits == [3, 2]
    assert caught.value.kind == "budget"
    assert gen.output_tokens_used == 5


def test_bounded_cloud_agent_generate_charges_failed_call_ceiling():
    def raw(_prompt, history=None):
        raw.last_usage = {}
        raise server.ModelCallError(
            "empty_response", "no assistant content", cloud=True,
        )

    raw.num_predict_override = None
    raw.last_usage = {}
    raw.last_response_meta = {}
    gen = server._bounded_cloud_agent_generate(
        raw, per_call_limit=4, total_budget=4,
    )

    with pytest.raises(server.ModelCallError, match="no assistant content"):
        gen("first")
    with pytest.raises(server.ModelCallError) as caught:
        gen("second")

    assert caught.value.kind == "budget"
    assert server._format_model_call_error(caught.value).startswith(
        "ERROR: hosted agent output budget exhausted:"
    )


def test_bounded_cloud_agent_generate_does_not_charge_preflight_cancel():
    limits = []
    calls = {"count": 0}

    def raw(_prompt, history=None):
        calls["count"] += 1
        limits.append(raw.num_predict_override)
        if calls["count"] == 1:
            raise server.ModelCallError(
                "cancelled", "cancelled before request", attempts=0, cloud=True,
            )
        raw.last_usage = {"tokens_out": 2}
        return "ok"

    raw.num_predict_override = None
    raw.last_usage = {}
    raw.last_response_meta = {}
    gen = server._bounded_cloud_agent_generate(
        raw, per_call_limit=4, total_budget=4,
    )

    with pytest.raises(server.ModelCallError) as caught:
        gen("cancel")
    assert caught.value.kind == "cancelled"
    assert gen("retry") == "ok"
    assert limits == [4, 4]
    assert gen.output_tokens_used == 2


def test_bounded_cloud_agent_generate_does_not_trust_zero_usage():
    def raw(_prompt, history=None):
        raw.last_usage = {"tokens_out": 0}
        return "x" * 1000

    raw.num_predict_override = None
    raw.last_usage = {}
    raw.last_response_meta = {}
    gen = server._bounded_cloud_agent_generate(
        raw, per_call_limit=4, total_budget=4,
    )

    assert gen("first") == "x" * 1000
    with pytest.raises(server.ModelCallError) as caught:
        gen("second")

    assert caught.value.kind == "budget"
    assert gen.output_tokens_used >= 250


def test_bounded_cloud_agent_generate_shares_reviewer_budget():
    state = {"spent": 0, "total": 5}

    def main_raw(_prompt, history=None):
        main_raw.last_usage = {"tokens_out": 4}
        return "main"

    def review_raw(_prompt, history=None):
        review_raw.last_usage = {"tokens_out": 1}
        return "r"

    for raw in (main_raw, review_raw):
        raw.num_predict_override = None
        raw.last_usage = {}
        raw.last_response_meta = {}
    main = server._bounded_cloud_agent_generate(
        main_raw, per_call_limit=4, total_budget=5, budget_state=state,
    )
    reviewer = server._bounded_cloud_agent_generate(
        review_raw, per_call_limit=2, total_budget=5, budget_state=state,
    )

    assert main("main") == "main"
    assert reviewer("review") == "r"
    with pytest.raises(server.ModelCallError) as caught:
        reviewer("over budget")

    assert caught.value.kind == "budget"
    assert state["spent"] == 5


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


def test_make_generate_retains_only_content_free_backend_measurements(monkeypatch):
    def fake_post(path, payload):
        return {
            "message": {"content": "ok"},
            "total_duration": 3_000_000,
            "load_duration": 1_000_000,
            "prompt_eval_count": 2,
            "prompt_eval_duration": 1_000_000,
            "eval_count": 1,
            "eval_duration": 1_000_000,
            "provider_secret": "must not cross the adapter boundary",
        }

    monkeypatch.setattr(server, "_post", fake_post)
    gen = server._make_generate("local-model", "", 0.1, 20, 2048)
    assert gen("hello") == "ok"
    assert gen.last_response_meta == {
        "done_reason": "",
        "total_duration": 3_000_000,
        "load_duration": 1_000_000,
        "prompt_eval_count": 2,
        "prompt_eval_duration": 1_000_000,
        "eval_count": 1,
        "eval_duration": 1_000_000,
    }


def test_serve_target_cloud_tier_requires_opt_in(monkeypatch):
    monkeypatch.delenv("SONDER_ALLOW_CLOUD", raising=False)
    model, cloud, augment, label = server._serve_target("cloud-code", None)
    assert model is None
    assert cloud is True
    assert augment is False
    assert label == "cloud-disabled"


def test_cloud_code_default_tracks_supported_hosted_model():
    assert server.DEFAULT_CLOUD_CODE_MODEL == "kimi-k2.7-code:cloud"


def test_cloud_general_default_tracks_supported_hosted_model():
    assert server.DEFAULT_CLOUD_GENERAL_MODEL == "glm-5.2:cloud"


def test_live_reload_migrates_old_cloud_general_unless_preserved(monkeypatch):
    monkeypatch.delenv("SONDER_PRESERVE_LEGACY_CLOUD_GENERAL", raising=False)
    monkeypatch.setitem(server.TIERS, "cloud-general", "gpt-oss:120b-cloud")
    server._refresh_live_cloud_tiers()
    assert server.TIERS["cloud-general"] == "glm-5.2:cloud"

    monkeypatch.setenv("SONDER_PRESERVE_LEGACY_CLOUD_GENERAL", "1")
    monkeypatch.setitem(server.TIERS, "cloud-general", "gpt-oss:120b-cloud")
    server._refresh_live_cloud_tiers()
    assert server.TIERS["cloud-general"] == "gpt-oss:120b-cloud"


def test_kimi_k3_extra_usage_402_falls_back_once(monkeypatch):
    calls = []

    def fake_chat(
        payload, *, model, cloud, timeout=None, cancel_check=None,
        accept_native_tool_calls=False, idempotent=False,
    ):
        calls.append((model, payload.get("think"), idempotent))
        if model == "kimi-k3:cloud":
            raise server.ModelCallError(
                "http", "extra usage balance is empty", status=402, cloud=True,
            )
        return {"message": {"content": "fallback-ok"}}, "fallback-ok"

    monkeypatch.setattr(server, "_chat_request", fake_chat)
    payload = {
        "model": "kimi-k3:cloud",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
        "think": True,
    }
    out, content, used_model = server._chat_request_with_cloud_fallback(
        payload, model="kimi-k3:cloud", timeout=30,
    )

    assert out["message"]["content"] == "fallback-ok"
    assert content == "fallback-ok"
    assert used_model == "kimi-k2.7-code:cloud"
    assert calls == [
        ("kimi-k3:cloud", True, True),
        ("kimi-k2.7-code:cloud", True, True),
    ]
    assert payload["model"] == "kimi-k3:cloud"
    assert payload["think"] is True


def test_kimi_k3_agent_fallback_preserves_compact_native_tool_flags(monkeypatch):
    calls = []

    def fake_chat(
        payload, *, model, cloud, timeout=None, cancel_check=None,
        accept_native_tool_calls=False, idempotent=False,
    ):
        calls.append((
            model, payload.get("think"), accept_native_tool_calls, idempotent,
        ))
        if model == "kimi-k3:cloud":
            raise server.ModelCallError(
                "http", "extra usage balance is empty", status=402, cloud=True,
            )
        return {"message": {"content": "fallback-ok"}}, "fallback-ok"

    monkeypatch.setattr(server, "_chat_request", fake_chat)
    payload = {
        "model": "kimi-k3:cloud",
        "messages": [{"role": "user", "content": "choose a tool"}],
        "stream": False,
        "think": True,
        "options": {"num_predict": 1200},
    }

    _, content, used_model = server._chat_request_with_cloud_fallback(
        payload,
        model="kimi-k3:cloud",
        accept_native_tool_calls=True,
        compact_cloud_reasoning=True,
    )

    assert content == "fallback-ok"
    assert used_model == "kimi-k2.7-code:cloud"
    assert calls == [
        ("kimi-k3:cloud", True, True, True),
        ("kimi-k2.7-code:cloud", False, True, True),
    ]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 503])
def test_kimi_k3_non_402_failure_never_falls_back(monkeypatch, status):
    calls = []

    def fake_chat(
        payload, *, model, cloud, timeout=None, cancel_check=None,
        accept_native_tool_calls=False, idempotent=False,
    ):
        calls.append((model, idempotent))
        raise server.ModelCallError("http", "rejected", status=status, cloud=True)

    monkeypatch.setattr(server, "_chat_request", fake_chat)
    with pytest.raises(server.ModelCallError) as caught:
        server._chat_request_with_cloud_fallback(
            {"model": "kimi-k3:cloud"}, model="kimi-k3:cloud", timeout=30,
        )
    assert caught.value.status == status
    assert calls == [("kimi-k3:cloud", True)]


def test_live_cloud_model_rewrites_known_retired_model():
    # A machine-wide SONDER_CLOUD_CODE set before qwen3-coder:480b-cloud's
    # 2026-07-15 retirement must not keep resurrecting the dead model.
    assert (
        server._live_cloud_model(
            "qwen3-coder:480b-cloud", server.DEFAULT_CLOUD_CODE_MODEL
        )
        == server.DEFAULT_CLOUD_CODE_MODEL
    )


def test_live_cloud_model_uses_the_callers_tier_specific_default():
    assert (
        server._live_cloud_model(
            "qwen3-coder:480b-cloud", server.DEFAULT_CLOUD_GENERAL_MODEL
        )
        == server.DEFAULT_CLOUD_GENERAL_MODEL
    )


def test_live_cloud_model_rewrite_is_case_insensitive():
    assert (
        server._live_cloud_model(
            "QWEN3-CODER:480B-CLOUD", server.DEFAULT_CLOUD_CODE_MODEL
        )
        == server.DEFAULT_CLOUD_CODE_MODEL
    )


def test_live_cloud_model_passes_through_other_overrides():
    assert (
        server._live_cloud_model("some-other:cloud", server.DEFAULT_CLOUD_CODE_MODEL)
        == "some-other:cloud"
    )


def test_live_cloud_model_falls_back_to_default_when_unset():
    assert (
        server._live_cloud_model(None, server.DEFAULT_CLOUD_CODE_MODEL)
        == server.DEFAULT_CLOUD_CODE_MODEL
    )
    assert (
        server._live_cloud_model("", server.DEFAULT_CLOUD_CODE_MODEL)
        == server.DEFAULT_CLOUD_CODE_MODEL
    )
    assert (
        server._live_cloud_model("   ", server.DEFAULT_CLOUD_CODE_MODEL)
        == server.DEFAULT_CLOUD_CODE_MODEL
    )


def test_tiers_cloud_code_survives_stale_retired_env_override(monkeypatch):
    # End-to-end: even with the stale env var this machine actually had set
    # (SONDER_CLOUD_CODE=qwen3-coder:480b-cloud), a fresh import must resolve
    # cloud-code to the live default rather than the retired model.
    monkeypatch.setenv("SONDER_CLOUD_CODE", "qwen3-coder:480b-cloud")
    reloaded = importlib.reload(server)
    try:
        assert reloaded.TIERS["cloud-code"] == reloaded.DEFAULT_CLOUD_CODE_MODEL
    finally:
        importlib.reload(server)


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


def test_structured_answer_forwards_decoder_schema_and_rejects_invalid_model_text(monkeypatch):
    seen = {}
    schema = {
        "type": "object", "required": ["ok"],
        "properties": {"ok": {"type": "boolean"}},
        "additionalProperties": False,
    }
    monkeypatch.setattr(
        server, "_serve_target",
        lambda *_args, **_kwargs: ("local-model", False, False, "fast"),
    )
    monkeypatch.setattr(server, "_context_requested", lambda _value: 2048)
    monkeypatch.setattr(server, "_build_system", lambda *_args, **_kwargs: "system")

    def fake_make_generate(*_args, **kwargs):
        seen["schema"] = kwargs["schema"]
        return lambda *_call_args: '{"ok":true}'

    monkeypatch.setattr(server, "_make_generate", fake_make_generate)
    assert server.structured_answer_with_history("return json", [], schema, tier="fast") == '{"ok":true}'
    assert seen["schema"] == schema

    monkeypatch.setattr(server, "_make_generate", lambda *_args, **_kwargs: lambda *_call_args: '{"ok":"no"}')
    with pytest.raises(server.ModelCallError, match="response_format validation failed"):
        server.structured_answer_with_history("return json", [], schema, tier="fast")


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
        memory_store.record_outcome_row(conn, "i1", "tests_passed", 1.0, source="machine")
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

    # The only outcome here is `tests_passed` -- the runtime grading its own
    # work. No caller has judged anything, so the status fails closed at
    # "watch": clean is not the same as measured good.
    assert data["status"] == "watch"
    assert data["reviewed_outcomes"] == 0
    assert data["outcome_coverage_percent"] == 100.0
    assert data["grounded_lessons"] == 1
    assert "sonder learning health" in text
    assert server.control_command("/learning") == text
    assert server.control_command("/metrics") == text


def test_learning_health_refreshes_only_loopback_embedding_provenance(monkeypatch):
    refreshed = []
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.learning_health, "build_report", lambda _conn: {"ok": True})

    class _Connection:
        def close(self):
            return None

    monkeypatch.setattr(server, "_open_db", _Connection)
    monkeypatch.setattr(server.embeddings, "endpoint_is_loopback", lambda _base: True)
    monkeypatch.setattr(
        server.embeddings, "refresh_runtime_revision", lambda: refreshed.append(True)
    )

    assert server.learning_health_data() == {"ok": True}
    assert refreshed == [True]

    monkeypatch.setattr(server.embeddings, "endpoint_is_loopback", lambda _base: False)
    assert server.learning_health_data() == {"ok": True}
    assert refreshed == [True]


def test_improvement_report_uses_refreshed_learning_health(monkeypatch):
    state = {
        "quality": {}, "interactions": 0, "outcomes": 0, "lessons": 10,
        "facts": 0, "outcome_coverage_percent": 0.0,
        "reviewed_outcomes": 0, "reviewed_positive_percent": 0.0,
    }
    calls = []
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "context_health_data", lambda **_kwargs: {"status": "healthy"})
    monkeypatch.setattr(
        server, "learning_health_data", lambda: calls.append(True) or state,
    )
    monkeypatch.setattr(server, "mcp_runtime_data", lambda: {})
    monkeypatch.setattr(server, "tool_manifest", lambda: "ground_artifact artifact_ground")
    monkeypatch.setattr(server, "cloud_allowed", lambda: True)

    report = server.improvement_report_data()

    assert report["interactions"] == 0
    assert calls == [True]


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
        memory_store.record_outcome_row(conn, "i1", "tests_passed", 1.0, source="caller")
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
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "1")
    server.activity_tracker.reset_for_tests()

    with server.activity_tracker.response_span("test", "create a file"):
        out = server.file_write("notes.txt", "one\ntwo\n", mode="create")

    latest = server.activity_tracker.snapshot()["latest"]
    assert "file write" in out
    assert latest["file_creates"] == 1
    assert latest["lines_added"] == 2
    assert latest["files"][0]["path"].endswith("notes.txt")
    feed = server.activity_tracker.execution_feed(server.activity_tracker.snapshot())
    changed = next(row for row in feed["events"] if row["kind"] == "file_change")
    assert changed["content_preview"]["text"] == "one\ntwo\n"
    assert changed["preview_kind"] == "content"


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


def test_mcp_runtime_format_redacts_paths_and_exposes_safe_restart_action():
    state = server.mcp_runtime_data()
    state.update({
        "status": "error",
        "last_error": "stale runtime source: loaded MCP file no longer exists",
        "provenance": {
            "pid": 42,
            "python": r"C:\Python\python.exe",
            "cwd": r"C:\deleted-worktree",
            "source_root": r"C:\deleted-worktree",
            "source_root_exists": False,
            "configured_runtime_root": r"C:\canonical\sonder-runtime",
            "configured_root_exists": True,
            "configured_root_ready": True,
            "issue": "stale_source_root",
            "recovery_action": (
                r"Restart/reconnect this process from the configured canonical root: "
                r"C:\canonical\sonder-runtime\sonder-runtime.cmd"
            ),
        },
    })

    text = server.format_mcp_runtime(state)

    assert "pid=42" in text
    assert "source root: missing" in text
    assert "provenance ERROR: stale_source_root" in text
    assert r"ACTION: Restart/reconnect" in text
    assert "SONDER_RUNTIME_ROOT" in text
    assert r"C:\deleted-worktree" not in text
    assert r"C:\canonical" not in text
    assert r"C:\Python" not in text


def test_debug_inspect_includes_full_mcp_provenance(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server, "admin_status", lambda token="": "admin status")
    monkeypatch.setattr(server, "master_status", lambda limit=10: "master status")
    monkeypatch.setattr(server, "system_improvement_report", lambda: "improvements")
    monkeypatch.setattr(server, "memory_quality_report", lambda sample_limit=3: "quality")
    monkeypatch.setattr(server, "status", lambda: "runtime status")
    monkeypatch.setattr(
        server,
        "format_mcp_runtime",
        lambda: "sonder MCP runtime\n  provenance ERROR: stale_source_root\n  ACTION: restart canonical",
    )

    text = server.debug_inspect()

    assert "sonder MCP runtime" in text
    assert "provenance ERROR: stale_source_root" in text
    assert "ACTION: restart canonical" in text


def test_mcp_refresh_command_reports_unavailable_source(monkeypatch):
    monkeypatch.setattr(
        server.mcp,
        "refresh_if_changed",
        lambda: {
            "reloaded": False,
            "surface_changed": False,
            "error": "stale runtime source: loaded MCP file is unavailable",
        },
    )
    monkeypatch.setattr(server, "format_mcp_runtime", lambda: "runtime detail")

    text = server._mcp_command("refresh")

    assert text.startswith(
        "MCP refresh failed closed: stale runtime source: "
        "loaded MCP file is unavailable"
    )
    assert text.endswith("runtime detail")


def test_mcp_runtime_format_never_echoes_injected_paths_or_credentials():
    secret = "credential-token-should-not-appear"
    state = {
        "status": "error",
        "enabled": True,
        "path": rf"C:\Users\secret\{secret}\server.py",
        "last_error": rf"OSError: C:\private\{secret}",
        "provenance": {
            "pid": 7,
            "python": rf"C:\private\{secret}\python.exe",
            "cwd": rf"C:\private\{secret}",
            "source_root": rf"C:\private\{secret}",
            "source_root_exists": False,
            "configured_runtime_root": rf"C:\private\{secret}",
            "configured_root_exists": False,
            "configured_root_ready": False,
            "issue": "stale_source_root",
            "recovery_action": rf"restart C:\private\{secret}",
        },
    }

    text = server.format_mcp_runtime(state)

    assert secret not in text
    assert "C:\\private" not in text
    assert "OSError: source refresh failed" in text
    assert all(ord(char) < 128 for char in text)


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


def test_unload_defers_while_fleet_model_call_is_active(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 2,
    )
    monkeypatch.setattr(
        server, "_post", lambda *args, **kwargs: pytest.fail("must not unload"),
    )

    output = server.unload("all")

    assert "unload deferred" in output
    assert "2 fleet model call(s)" in output


def test_unload_deduplicates_verifies_and_cleans_discovery_probes(monkeypatch):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(
        server.master_orchestrator, "active_model_call_count", lambda: 0,
    )
    monkeypatch.setattr(
        server,
        "TIERS",
        {
            "fast": "small:latest",
            "code": "sonder:latest",
            "general": "sonder:latest",
            "cloud-code": "hosted-cloud",
        },
    )
    posted = []
    monkeypatch.setattr(
        server,
        "_post",
        lambda path, payload: posted.append((path, payload)) or {"done": True},
    )
    monkeypatch.setattr(server, "_get", lambda path: {"models": []})
    monkeypatch.setattr(
        server.ollama_lifecycle,
        "cleanup_orphaned_discovery_probes",
        lambda **_kwargs: {
            "terminated": [41, 42],
            "terminated_model_runners": [],
            "protected_model_runners": [],
            "errors": [],
        },
    )

    output = server.unload("all")

    assert [payload["model"] for _path, payload in posted] == [
        "small:latest", "sonder:latest",
    ]
    assert all(payload["keep_alive"] == 0 for _path, payload in posted)
    assert "residency confirmed clear" in output
    assert "41, 42" in output


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


def test_orchestrator_agent_worker_raises_host_generated_errors(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        server,
        "_agent_impl",
        lambda *args, **kwargs: calls.append((args, kwargs)) or
        "ERROR: model decision failed",
    )

    worker = server._orchestrator_agent_worker("code", str(tmp_path))

    try:
        worker("inspect repository", str(tmp_path))
    except RuntimeError as error:
        assert "model decision failed" in str(error)
    else:
        raise AssertionError("host-generated agent error was treated as success")

    assert calls
    assert calls[0][1]["auto_checklist"] is True
    assert calls[0][1]["project"] == str(tmp_path.resolve())


def test_orchestrator_agent_worker_propagates_existing_repository_root(
    monkeypatch, tmp_path,
):
    calls = []
    monkeypatch.setattr(
        server,
        "_agent_impl",
        lambda *args, **kwargs: calls.append((args, kwargs)) or
        _host_repo_result(tmp_path),
    )

    worker = server._orchestrator_agent_worker("code", str(tmp_path))
    prompt = "Repository: %s\nRead-only inspection." % tmp_path
    result = worker(prompt, str(tmp_path))

    assert isinstance(result, server.master_orchestrator.RepositoryWorkerResult)
    assert result.output.startswith("grounded result")
    assert calls
    assert calls[0][1]["project"] == str(tmp_path.resolve())
    assert calls[0][1]["auto_checklist"] is True
    assert calls[0][1]["require_file_evidence"] is True
    assert calls[0][1]["read_only"] is True


def test_repo_master_uses_cancel_aware_worker_and_persists_host_failure(
    monkeypatch, tmp_path,
):
    calls = []

    def fail_agent(*args, **kwargs):
        calls.append(kwargs)
        return "ERROR: repository agent failed"

    monkeypatch.setattr(server, "_agent_impl", fail_agent)

    out = server.master_orchestrate(
        "Inspect current files.",
        mode="inline",
        project=str(tmp_path),
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
    monkeypatch.setenv("SONDER_EXECUTION_FEED_DETAIL", "1")

    with server.activity_tracker.response_span("offload", "plain") as response:
        output = server.offload("plain", tier="fast", learn=False)

        assert output == "plain output"
        assert response["model_calls"] == 1
        assert response["tokens_in"] == 9
        assert response["tokens_out"] == 3

    feed = server.activity_tracker.execution_feed(server.activity_tracker.snapshot())
    model = next(row for row in feed["events"] if row["kind"] == "model_call")
    assert model["request_preview"]["text"] == "plain"
    assert model["response_preview"]["text"] == "plain output"


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


def test_master_orchestrate_fleet_returns_monitorable_background_id(monkeypatch):
    calls = []
    monkeypatch.setattr(server.master_orchestrator, "max_agents", lambda: 12)
    monkeypatch.setattr(
        server.master_orchestrator,
        "start_delegated",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "master_id": "master-background",
            "agents": ["agent-one", "agent-two"],
            "worker_slots": 2,
            "output": "RUNNING",
            "background": True,
        },
    )

    out = server.master_orchestrate("inspect risks", mode="fleet", agents=2)

    assert "master orchestration started" in out
    assert "master-background" in out
    assert "master_status()" in out
    assert "master_cancel('master-background')" in out
    assert calls
    assert calls[0][1]["agents"] == 2


def test_master_orchestrate_auto_fleet_preserves_explicit_agent_count(monkeypatch):
    calls = []
    monkeypatch.setattr(server.master_orchestrator, "max_agents", lambda: 32)
    monkeypatch.setattr(
        server.master_orchestrator,
        "start_delegated",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "master_id": "master-explicit-auto",
            "agents": ["agent-%d" % i for i in range(kwargs["agents"])],
            "worker_slots": 2,
            "output": "RUNNING",
            "background": True,
        },
    )

    out = server.master_orchestrate(
        "run a fleet audit of orchestration safety",
        mode="delegate",
        agents=8,
    )

    assert "agents=8" in out
    assert calls[0][1]["agents"] == 8
    assert calls[0][1]["metadata"]["mode"] == "fleet"


def test_master_orchestrate_fleet_without_agent_count_uses_ceiling(monkeypatch):
    calls = []
    monkeypatch.setattr(server.master_orchestrator, "max_agents", lambda: 12)
    monkeypatch.setattr(
        server.master_orchestrator,
        "start_delegated",
        lambda *args, **kwargs: calls.append((args, kwargs)) or {
            "master_id": "master-automatic-ceiling",
            "agents": ["agent-%d" % i for i in range(kwargs["agents"])],
            "worker_slots": 2,
            "output": "RUNNING",
            "background": True,
        },
    )

    out = server.master_orchestrate("inspect risks", mode="fleet")

    assert "agents=12" in out
    assert calls[0][1]["agents"] == 12


def test_master_orchestrate_fleet_persists_explicit_agent_count(monkeypatch):
    # tests/conftest.py pins SONDER_FLEET_DB to a disposable test-only database;
    # this exercises real durable persistence without touching the live ledger.
    monkeypatch.setattr(server.master_orchestrator, "max_agents", lambda: 32)
    monkeypatch.setattr(
        server.master_orchestrator, "parallel_worker_slots", lambda requested: 2,
    )
    monkeypatch.setattr(
        server,
        "_orchestrator_worker",
        lambda *args, **kwargs: (lambda prompt: "audited"),
    )

    out = server.master_orchestrate(
        "compare orchestration safety tradeoffs",
        mode="fleet",
        agents=8,
    )

    deadline = time.time() + 2.0
    snapshot = server.master_orchestrator.snapshot(include_finished=True, limit=100)
    while snapshot["active_agents"] and time.time() < deadline:
        time.sleep(0.01)
        snapshot = server.master_orchestrator.snapshot(include_finished=True, limit=100)

    assert snapshot["active_agents"] == 0
    masters = [row for row in snapshot["agents"] if row["role"] == "master"]
    assert len(masters) == 1
    master = masters[0]
    children = [
        row for row in snapshot["agents"] if row["parent_id"] == master["id"]
    ]
    assert "agents=8" in out
    assert master["requested_agents"] == 8
    assert len(children) == 8


def test_master_orchestrate_schema_marks_zero_as_automatic_agent_count():
    schema = server.mcp._tool_manager.get_tool("master_orchestrate").parameters

    assert schema["properties"]["agents"]["default"] == 0
    assert schema["properties"]["worker_cap"]["default"] == 0
    assert schema["properties"]["project"]["default"] == ""


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


def test_master_retry_preserves_persisted_repository_scope(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        server.master_orchestrator,
        "recovery_candidate",
        lambda selector: {
            "id": "master-scoped",
            "status": "interrupted",
            "task": "finish the repository review",
            "mode": "delegate",
            "requested_agents": 2,
            "tier": "code",
            "project": str(tmp_path),
        },
    )
    monkeypatch.setattr(
        server, "master_orchestrate",
        lambda **kwargs: calls.append(kwargs) or "retry complete",
    )

    server.master_retry("master-scoped")

    assert calls[0]["project"] == str(tmp_path)


def test_master_retry_recovers_scope_from_legacy_files_metadata(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(
        server.master_orchestrator,
        "recovery_candidate",
        lambda selector: {
            "id": "master-legacy-scoped",
            "status": "interrupted",
            "task": "finish the repository review",
            "mode": "delegate",
            "requested_agents": 2,
            "tier": "code",
            "project": "",
            "files": [str(tmp_path)],
        },
    )
    monkeypatch.setattr(
        server, "master_orchestrate",
        lambda **kwargs: calls.append(kwargs) or "retry complete",
    )

    server.master_retry("master-legacy-scoped")

    assert calls[0]["project"] == str(tmp_path)


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
    status = server.master_status()
    assert "latest completed master result [" in status
    assert "  task: find risks\naudited merge" in status


def test_master_orchestrate_uses_tool_agent_for_repo_inspection(monkeypatch, tmp_path):
    calls = []

    def grounded_agent(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return _host_repo_result(kwargs["project"], "grounded agent output")

    monkeypatch.setattr(
        server,
        "_agent_impl",
        grounded_agent,
    )
    monkeypatch.setattr(server, "_offload_impl", lambda prompt, **kwargs: "audited merge")

    # A real, existing repository root keeps the fail-closed resolver satisfied
    # on every host; the old machine-specific absolute literal only resolved on the
    # author's Windows box and errored the delegated lane on Linux CI.
    out = server.master_orchestrate(
        "Repository: %s. Review current uncommitted files using local file-reading tools." % tmp_path,
        mode="delegate",
        agents=4,
    )

    assert "audited merge" in out
    assert len(calls) == 4
    assert all(options["require_file_evidence"] for _, options in calls)
    assert all(options["read_only"] for _, options in calls)
    assert all(options["include_evidence"] for _, options in calls)


def test_master_explicit_project_forces_scoped_repository_fleet(
    monkeypatch, tmp_path,
):
    agent_calls = []
    audit_prompts = []
    monkeypatch.setattr(
        server.master_orchestrator, "parallel_worker_slots", lambda requested: 1,
    )

    def grounded_agent(prompt, **kwargs):
        agent_calls.append((prompt, kwargs))
        return _host_repo_result(kwargs["project"])

    def audit(prompt, **kwargs):
        audit_prompts.append(prompt)
        return "requested-project merge"

    monkeypatch.setattr(server, "_agent_impl", grounded_agent)
    monkeypatch.setattr(server, "_offload_impl", audit)

    out = server.master_orchestrate(
        "Find the highest-impact implementation gaps.",
        mode="delegate",
        agents=2,
        project=str(tmp_path),
    )
    snapshot = server.master_orchestrator.snapshot(limit=20)
    scoped_rows = [
        row for row in snapshot["agents"]
        if row["role"] in {"master", "agent"}
    ]
    expected = str(tmp_path.resolve())

    assert "=== HOST AGGREGATION SCOPE ===" in out
    assert "requested-project merge" in out
    assert len(agent_calls) == 2
    assert all(kwargs["project"] == expected for _, kwargs in agent_calls)
    assert all(kwargs["return_host_receipt"] is True for _, kwargs in agent_calls)
    assert audit_prompts and "HOST REPOSITORY SCOPE: %s" % expected in audit_prompts[0]
    assert scoped_rows and {row["project"] for row in scoped_rows} == {expected}


def test_master_repository_task_without_explicit_root_fails_before_agent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        server, "_agent_impl", lambda *a, **k: calls.append((a, k)) or "unexpected",
    )

    out = server.master_orchestrate(
        "Inspect the current repository files for feature gaps.",
        mode="delegate",
        agents=2,
    )

    assert out.startswith("EVIDENCE_REQUIRED:")
    assert "no cwd fallback" in out
    assert calls == []


def test_admin_register_login_and_cot_denial(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "admin.db"))

    registered = server.admin_register("owner", "password123")
    login = server.admin_login("owner", "password123")

    assert "role=admin" in registered
    assert "token:" in login
    token = login.split("token: ", 1)[1].strip()
    assert "owner role=admin" in server.admin_whoami(token)
    # An admin token is not an opt-in. Exposure needs SONDER_ALLOW_PRIVATE_COT
    # plus an explicit allow rule; with neither set this refuses regardless of
    # who is asking. See tests/test_private_cot_opt_in.py for the opted-in side.
    monkeypatch.delenv("SONDER_ALLOW_PRIVATE_COT", raising=False)
    denial = server.admin_private_chain_of_thought(token)
    assert "hidden private chain-of-thought cannot be exposed" in denial
    assert "SONDER_ALLOW_PRIVATE_COT" in denial


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


def test_campaign_output_match_requires_exact_not_substring():
    """Substring containment let a chatty answer false-pass: "The result of 12
    + 30 is 42." contains "42" and was recorded tests_passed even though the
    task says print exactly 42. Short numeric expectations are the exploitable
    case; the match is now line-exact after whitespace normalisation."""
    match = server._campaign_output_matches
    # Correct output passes, tolerating trailing whitespace and line endings.
    assert match("42", "42")
    assert match("42\n", "42")
    assert match(" 42 \n", "42")
    assert match("1\r\n2\r\n3\r\n", "1\n2\n3")
    assert match("\nsonder-ok\n", "sonder-ok")
    # Prose that merely embeds the value is rejected.
    assert not match("The result of 12 + 30 is 42.", "42")
    assert not match("answer: 20", "20")
    assert not match("The order is d a b c", "d a b c")
    # Extra lines around the expected output are rejected.
    assert not match("0\n1\n2\n3", "1\n2\n3")
    # The earlier valid/invalid collision stays dead under the new matcher.
    assert not match("valid\ninvalid\ninvalid", "ok\nbad\nbad")


def test_offload_context_window_follows_the_context_policy(monkeypatch):
    """The session path already asked context_policy for its window; the
    offload path hardcoded 4096, ignoring the policy and its env knobs. That
    cost real capability - an autopilot run inspecting a 524 KB file looped on
    search because the file was 32x its window."""
    seen = {}

    def fake_options(temperature, num_predict, num_ctx):
        seen["num_ctx"] = num_ctx
        return {}

    monkeypatch.setattr(server, "_local_model_options", fake_options)
    monkeypatch.setattr(server, "_refresh_live_cloud_tiers", lambda: None)
    monkeypatch.setattr(
        server, "_serve_target", lambda tier, strict=None: (None, False, False, None),
    )
    # A caller that passes nothing gets the policy's window, not a literal.
    try:
        server._offload_impl("task", tier="code")
    except Exception:
        pass
    # This is the whole point of the test: the window handed to the model is
    # the one the policy computed. Asserting a fixed number instead only pins
    # whatever this host happens to be tuned to -- and did. The literal here
    # was 8192, which stopped being the default once the window started being
    # sized from the KV cache type, so the test failed on every machine
    # running a quantised KV cache and passed everywhere else.
    assert "num_ctx" in seen, "offload never consulted _local_model_options"
    assert seen["num_ctx"] == server.context_policy.native()
    # An explicit value still wins over the policy default.
    assert server.context_policy.native(4096) == 4096


def test_campaign_environment_failure_is_not_a_model_failure():
    # Host toolchain breakage (grounding._missing's sentinel) must not be
    # recorded against the model.
    assert server._campaign_environment_failure("missing runtime/compiler: node")
    assert server._campaign_environment_failure("missing runtime/compiler: csc")
    # Real model failures still count.
    assert not server._campaign_environment_failure(
        "wrong output; expected exactly '42', got '41'"
    )
    assert not server._campaign_environment_failure("no python code block returned")
    assert not server._campaign_environment_failure("(timed out after 8s)")
    assert not server._campaign_environment_failure("")
    assert not server._campaign_environment_failure(None)


def _improvement_report_text(**overrides):
    base = {
        "score": 100, "interactions": 7865, "outcomes": 6831,
        "reviewed_positive_percent": 52.7, "reviewed_outcomes": 186,
        "autograded_outcomes": 6645,
        # acceptance_percent is the caller-judged rate now; the blend is read
        # from learning_health, which is where it is actually computed.
        "acceptance_percent": 52.7, "acceptance_basis": "reviewed",
        "learning_health": {
            "outcome_coverage_percent": 86.8,
            "autograded_positive_percent": 97.3,
            "positive_percent": 96.1,
        },
        "memory_quality": {}, "autopilot": {}, "mcp_runtime": {}, "issues": [],
    }
    base.update(overrides)
    return server.format_improvement_report(base)


def test_improvement_report_never_shows_the_blended_rate_alone():
    """The blended positive rate is dominated by the runtime marking its own
    curriculum. Shown by itself it reads as a quality score, which it is not:
    this report displayed 96.1% positive and 100/100 readiness while
    caller-judged work sat at 52.7% and learning_health said "watch"."""
    text = _improvement_report_text()
    assert "caller-judged: 52.7% of 186 reviewed" in text
    assert "autograded: 97.3% of 6645" in text
    # The blended number may appear, but only alongside its two components.
    blended_line = [ln for ln in text.split("\n") if "96.1" in ln]
    assert blended_line, "blended rate should still be reported"
    assert "caller-judged" in blended_line[0]


def test_improvement_report_flags_a_low_caller_judged_rate(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(
        server.learning_health, "build_report",
        lambda conn: {
            "quality": {}, "interactions": 500, "outcomes": 400,
            "lessons": 50, "facts": 5, "positive_percent": 96.1,
            "outcome_coverage_percent": 80.0,
            "reviewed_outcomes": 186, "reviewed_positive_percent": 52.7,
            "autograded_outcomes": 214, "autograded_positive_percent": 97.3,
        },
    )
    report = server.improvement_report_data()
    titles = " ".join(i["title"] for i in report["issues"])
    assert "Caller-judged work succeeds 52.7%" in titles
    # A 52.7% hit rate must cost readiness rather than scoring a clean 100.
    assert report["score"] < 100


def test_improvement_report_flags_when_nothing_has_been_judged(monkeypatch, tmp_path):
    """Autograded outcomes cannot say whether delegated work is any good."""
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(
        server.learning_health, "build_report",
        lambda conn: {
            "quality": {}, "interactions": 500, "outcomes": 400,
            "lessons": 50, "facts": 5, "positive_percent": 99.0,
            "outcome_coverage_percent": 80.0,
            "reviewed_outcomes": 2, "reviewed_positive_percent": 100.0,
            "autograded_outcomes": 398, "autograded_positive_percent": 99.0,
        },
    )
    report = server.improvement_report_data()
    titles = " ".join(i["title"] for i in report["issues"])
    assert "judged by a caller" in titles
    assert report["score"] <= 75


def _improvement_report_with(monkeypatch, tmp_path, **learning):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    state = {
        "quality": {}, "interactions": 500, "outcomes": 400,
        "lessons": 50, "facts": 5, "outcome_coverage_percent": 80.0,
    }
    state.update(learning)
    monkeypatch.setattr(
        server.learning_health, "build_report", lambda conn: state,
    )
    return server.improvement_report_data()


def test_improvement_report_has_no_acceptance_rate_until_work_is_judged(
    monkeypatch, tmp_path,
):
    """`acceptance_percent` was the blended rate wearing a caller-judgement
    name. 398 rows of the runtime grading itself and 2 real judgements
    published "acceptance: 99.0%". learning_health's gate stopped believing the
    blend; this surface was still republishing it, which is exactly how a fix
    to one reader leaves the shape alive on a sibling."""
    report = _improvement_report_with(
        monkeypatch, tmp_path,
        positive_percent=99.0, reviewed_outcomes=2,
        reviewed_positive_percent=100.0, autograded_outcomes=398,
        autograded_positive_percent=99.0,
    )

    assert report["acceptance_percent"] is None, "an unjudged store has no acceptance rate"
    assert report["acceptance_basis"] == "unmeasured"


def test_improvement_report_uses_the_canonical_twenty_review_sample(monkeypatch, tmp_path):
    report = _improvement_report_with(
        monkeypatch, tmp_path,
        positive_percent=99.0, reviewed_outcomes=25,
        reviewed_positive_percent=88.0, autograded_outcomes=375,
        autograded_positive_percent=99.0,
    )

    assert report["acceptance_percent"] == 88.0
    assert report["acceptance_basis"] == "reviewed"
    assert report["score"] > 75


def test_improvement_report_acceptance_is_the_caller_judged_rate_once_measurable(
    monkeypatch, tmp_path,
):
    """Fail-closed, not permanently silent: with a sample that can carry it,
    the number published is the honest one -- 88, never the 92 blend."""
    report = _improvement_report_with(
        monkeypatch, tmp_path,
        positive_percent=92.0, reviewed_outcomes=200,
        reviewed_positive_percent=88.0, autograded_outcomes=200,
        autograded_positive_percent=96.0,
    )

    assert report["acceptance_percent"] == 88.0
    assert report["acceptance_basis"] == "reviewed"


def test_improvement_report_does_not_render_an_unjudged_store_as_zero_percent():
    """"0.0% of 0 reviewed" reads as total failure when it means nobody looked.
    Unmeasured has to say so in words."""
    text = _improvement_report_text(
        acceptance_percent=None, acceptance_basis="unmeasured",
        reviewed_outcomes=0, reviewed_positive_percent=0.0,
    )

    assert "caller-judged: unmeasured" in text
    assert "caller-judged: 0.0%" not in text


def test_improvement_report_stays_quiet_when_review_is_healthy(monkeypatch, tmp_path):
    monkeypatch.setattr(server, "_DB_PATH", str(tmp_path / "mem.db"))
    monkeypatch.setattr(
        server.learning_health, "build_report",
        lambda conn: {
            "quality": {}, "interactions": 500, "outcomes": 400,
            "lessons": 50, "facts": 5, "positive_percent": 92.0,
            "outcome_coverage_percent": 80.0,
            "reviewed_outcomes": 200, "reviewed_positive_percent": 88.0,
            "autograded_outcomes": 200, "autograded_positive_percent": 96.0,
        },
    )
    report = server.improvement_report_data()
    titles = " ".join(i["title"] for i in report["issues"])
    assert "Caller-judged work succeeds" not in titles


def test_runtime_identity_names_the_serving_model_only(monkeypatch):
    """Asked what it was, a local 7B answered "based on OpenAI's GPT-4
    architecture, approximately 175 billion parameters". Those facts were not in
    the prompt, so answering was recall -- the axis this model class is worst on.

    Pins BOTH the fix and its first bad version: listing every tier gave the
    model a menu and it picked `kimi-k2.7-code:cloud` while actually running on
    `sonder:latest`. A block written to remove a guess must not add a new one.
    """
    monkeypatch.setattr(server, "TIERS", {
        "code": "sonder:latest",
        "reasoning": "deepseek-r1:7b",
        "cloud-code": "kimi-k2.7-code:cloud",
    }, raising=False)
    block = server._runtime_identity_block("sonder:latest")

    assert "sonder:latest" in block
    # The regression: no other tier's model may appear, or the model chooses.
    assert "kimi-k2.7-code:cloud" not in block
    assert "deepseek-r1:7b" not in block
    assert "GPT-4" in block and "NOT" in block
    # It must license "I don't know" rather than invite a plausible number.
    assert "parameter count" in block
    assert "do not know" in block.lower()


def test_runtime_identity_degrades_to_silence_and_never_raises(monkeypatch):
    """This runs on every request, so it must degrade rather than break one.

    This test previously asserted the OPPOSITE: that an unknown model falls
    back to `TIERS["code"]`. That fallback was the #49 defect -- with the only
    writer of `_ACTIVE_MODEL_HINT` uncalled, the fallback fired on 100% of
    requests and asserted the code tier at models that were not it. The test
    encoded the defect as the requirement, so wiring the block correctly would
    have failed it. Silence is the correct degradation.
    """
    monkeypatch.setattr(server, "TIERS", {"code": "sonder:latest"}, raising=False)
    assert server._runtime_identity_block("") == ""
    assert server._runtime_identity_block(None) == ""

    # No usable name anywhere: emit nothing rather than a half-stated fact.
    monkeypatch.setattr(server, "TIERS", {}, raising=False)
    assert server._runtime_identity_block("") == ""


def test_build_system_puts_the_identity_facts_first():
    """Wiring test. The block existing is not the same as it being sent --
    the first version of this change was verified by reading the block and
    still shipped a model that named the wrong tier."""
    built = server._build_system("", False, "", model="sonder:latest")
    assert built.startswith("Facts about what is serving this request")
    # Omitting the model must drop the block, not guess one.
    assert not server._build_system(
        "", False, "").startswith("Facts about what is serving")


# --- #49: the identity block must name the model the call site resolved ----
#
# `_ACTIVE_MODEL_HINT` was only ever written by `_resolve_model_and_system`,
# which has zero callers anywhere in the tree. So the hint was always "" and
# every request fell through to `TIERS["code"]` -- the block that exists to
# stop a model guessing its identity asserted a guess, authoritatively, to
# models that were not that model.


def test_execution_router_is_told_the_router_model_not_the_code_tier(monkeypatch):
    """The defect, at a real call site.

    `_execution_route_model` resolves the `fast` tier and then builds a system
    prompt. Before the fix that prompt asserted `TIERS["code"]` was serving the
    request, to a model that was not it.
    """
    captured = {}

    def fake_make_generate(model, system, *args, **kwargs):
        captured["model"] = model
        captured["system"] = system
        return lambda prompt, history=None: (
            '{"mode":"workbench","tier":"fast","reason":"one step","confidence":0.6}'
        )

    monkeypatch.setattr(
        server, "TIERS", {"code": "sonder:latest", "fast": "qwen2.5:3b"}, raising=False)
    monkeypatch.setattr(server, "LOCAL_TIERS", ("code", "fast"), raising=False)
    monkeypatch.setattr(
        server, "_serve_target",
        lambda *_a, **_k: ("qwen2.5:3b", False, False, "fast"))
    monkeypatch.setattr(server, "_make_generate", fake_make_generate)

    server._execution_route_model("inspect and implement")

    assert captured["model"] == "qwen2.5:3b"
    assert "qwen2.5:3b" in captured["system"]
    # The regression: the code tier must not be asserted at a fast-tier model.
    assert "sonder:latest" not in captured["system"]


def test_identity_block_omits_the_claim_when_no_model_is_known(monkeypatch):
    """A wrong identity asserted as authoritative is worse than none.

    The old fallback guessed `TIERS["code"]` whenever the hint was empty --
    which, with no caller setting it, was every single request.
    """
    monkeypatch.setattr(server, "TIERS", {"code": "sonder:latest"}, raising=False)
    assert server._runtime_identity_block("") == ""


def test_identity_block_does_not_place_a_cloud_model_on_this_machine():
    """Cloud tiers reach _build_system too; the block must not claim a hosted
    model is running locally."""
    _LOCAL_CLAIM = "served by Ollama on this machine"
    block = server._runtime_identity_block("kimi-k2.7-code:cloud", cloud=True)
    assert "kimi-k2.7-code:cloud" in block
    assert _LOCAL_CLAIM not in block
    assert "not on this machine" in block
    local = server._runtime_identity_block("sonder:latest", cloud=False)
    assert _LOCAL_CLAIM in local
