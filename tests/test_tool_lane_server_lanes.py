"""The tool-lane deny list through the real ``server`` lanes, with the model call faked.

Nothing here reaches a model: the text generators and request helpers are
replaced by recorders, the model catalog is a dictionary, and every lane that
must refuse is asserted to have made zero model calls and started no work.
The provider and tier variables are cleared per test and the runtime policy
file lives under ``tmp_path``.
"""
from __future__ import annotations

import contextlib
import json
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters import tool_lane_guard as guard
from tests.support.tool_lane import (
    ALLOWED,
    BLOCKED,
    DENIED,
    HF_ORNITH,
    HF_QWEN,
    RUNNABLE,
    UNKNOWN,
    UNKNOWN_OPENAI,
    UNREADABLE,
    clear_environment,
    served_refusal,
    write_policy,
)

DENIED_SPELLINGS = (
    DENIED,
    "Astra-Cyber-9B",
    "astra-cyber-27b:latest",
    "library/astra-cyber-27b",
    HF_QWEN,
    HF_ORNITH,
)
MODEL_CALL_NAMES = ("_chat_request", "_make_generate", "_make_tier_generate", "_post_model")


# ---- fixtures ----------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    policy_path = tmp_path / "runtime_policy.json"
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(policy_path))
    before = dict(server.TIERS)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", server._RUNTIME_POLICY)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    server._refresh_runtime_policy(create=False)
    for tier in ("fast", "code", "general"):
        server.TIERS[tier] = ALLOWED
    yield policy_path
    server.TIERS.clear()
    server.TIERS.update(before)


@pytest.fixture
def policy_file(isolated):
    def write(payload):
        write_policy(isolated, payload)
        return server._refresh_runtime_policy(create=False)

    return write


@pytest.fixture
def catalog(monkeypatch):
    names = {name.strip().casefold(): name for name in DENIED_SPELLINGS}
    names[ALLOWED] = ALLOWED

    def record(selector):
        key = str(selector or "").strip().casefold()
        return (names[key], {"name": names[key]}) if key in names else None

    monkeypatch.setattr(server, "resolve_discovered_model_record", record)
    monkeypatch.setattr(server, "_fanout_nonchat_reason", lambda record: "")
    return names


@pytest.fixture
def model_calls(monkeypatch):
    """Record (and fail) every way the server can reach a model."""
    calls = []

    def recorder(name):
        def fake(*args, **kwargs):
            calls.append(name)
            raise RuntimeError(f"model call {name} must not happen")

        return fake

    for name in MODEL_CALL_NAMES:
        monkeypatch.setattr(server, name, recorder(name))
    return calls


@pytest.fixture
def work_started(monkeypatch):
    """Record any text-entry or game work that a refusing lane must not start."""
    started = []

    def recorder(name):
        def fake(*args, **kwargs):
            started.append(name)
            raise RuntimeError(f"{name} must not start")

        return fake

    monkeypatch.setattr(server, "sonder", recorder("sonder"))
    monkeypatch.setattr(server, "ensemble_answer", recorder("ensemble_answer"))
    monkeypatch.setattr(server.game_forge, "prepare_project", recorder("prepare_project"))
    return started


def bridge(monkeypatch, provider, **env):
    """Bind the code tier to a non-Ollama provider (environment only)."""
    monkeypatch.setenv("SONDER_CODE_PROVIDER", provider)
    for name, value in env.items():
        monkeypatch.setenv(name, value)


def run_gate(model, tier_label, **extra):
    common = {"iid": None, "prompt": "p", "history": [], "system": "", "temperature": 0.2,
              "num_predict": 64, "num_ctx": 0, "cloud": False}
    return server._gate_answer_code(RUNNABLE, model=model, tier_label=tier_label, **common, **extra)


@pytest.fixture
def code_runs(monkeypatch):
    """The chat code gate with execution recorded; the repair generator must not be built."""
    runs = []

    def run_code_detail(code, **kwargs):
        runs.append(code)
        raise RuntimeError("stop after recording")

    monkeypatch.setattr(server, "_code_gate_enabled", lambda: True)
    monkeypatch.setattr(server.grounding, "run_code_detail", run_code_detail)
    monkeypatch.setattr(server.grounding, "run_code", lambda *a, **k: runs.append(a))
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("repair generator built")))
    return runs


# ---- _serve_target / chat_target ---------------------------------------------

@pytest.mark.parametrize("name", DENIED_SPELLINGS)
def test_serve_target_refuses_a_denied_name_by_default_and_resolves_it_for_chat(catalog, name):
    assert server._serve_target(name, None) == (None, False, False, "tool-lane-denied")
    assert server._serve_target(name, False)[3] == "tool-lane-denied"
    assert server._serve_target(name, None, chat_only=True) == (name, False, False, "model:" + name)
    assert guard.chat_target(server._serve_target, name, None) == (name, False, False, "model:" + name)


def test_serve_target_default_is_the_refusing_one_even_with_odd_spelling(catalog):
    assert server._serve_target("  Astra-Cyber-27B ", False)[3] == "tool-lane-denied"
    assert server._serve_target("ASTRA-CYBER-27B:LATEST", None)[3] == "tool-lane-denied"


def test_serve_target_leaves_an_ordinary_model_alone(catalog):
    expected = (ALLOWED, False, False, "model:" + ALLOWED)
    assert server._serve_target(ALLOWED, None) == expected
    assert server._serve_target(ALLOWED, None, chat_only=True) == expected
    assert guard.chat_target(server._serve_target, ALLOWED, None) == expected


def test_serve_target_still_resolves_tiers_and_the_default_route(catalog):
    assert server._serve_target("code", None)[0] == ALLOWED
    assert server._serve_target("code", None)[3] == "code"
    assert server._serve_target("", None)[0] == ALLOWED
    assert server._serve_target("sonder", None)[3] != "tool-lane-denied"
    assert server._serve_target("no-such-model-xyz", None)[3] is None  # unknown stays unknown


def test_chat_target_resolves_again_only_for_a_refused_or_unknown_target():
    seen = []

    def fake(tier, strict, **kwargs):
        seen.append((tier, strict, kwargs))
        if kwargs:
            return ("m", False, False, "model:m")
        return {"denied": ("m", False, False, guard.DENIED_LABEL),
                "unknown": (None, False, True, None),
                "cloud": (None, True, False, "cloud-disabled")}.get(tier, ("m", False, False, "model:m"))

    assert guard.chat_target(fake, "ok", True)[3] == "model:m"
    assert seen == [("ok", True, {})]  # an ordinary selector keeps the two-argument call
    seen.clear()
    assert guard.chat_target(fake, "cloud", True)[3] == "cloud-disabled"
    assert seen == [("cloud", True, {})]  # any other refusal is final
    for selector in ("denied", "unknown"):
        seen.clear()
        assert guard.chat_target(fake, selector, None)[3] == "model:m"
        assert seen == [(selector, None, {}), (selector, None, {"chat_only": True})]
    # The narrower rule, for a resolver that also serves tool lanes.
    seen.clear()
    assert guard.chat_target(fake, "unknown", None, pool_wide=False)[3] is None
    assert seen == [("unknown", None, {})]
    seen.clear()
    assert guard.chat_target(fake, "denied", None, pool_wide=False)[3] == "model:m"
    assert seen == [("denied", None, {}), ("denied", None, {"chat_only": True})]


# ---- _agent_turn -------------------------------------------------------------

@pytest.mark.parametrize("name", DENIED_SPELLINGS)
def test_agent_turn_refuses_a_denied_name_before_any_model_call(catalog, model_calls, name):
    assert server._agent_turn("list files", tier=name) == BLOCKED % name
    assert server._agent_impl("list files", tier=name) == BLOCKED % name
    # The MCP wrappers may fold the selector's case before they refuse it.
    assert (BLOCKED % name).lower() in server.agent("list files", tier=name).lower()
    assert (BLOCKED % name).lower() in server.workbench_agent("list files", tier=name).lower()
    assert model_calls == []


def test_agent_turn_refuses_a_prepared_target(catalog, model_calls, monkeypatch):
    import sonder_runtime.bootstrap.prepared_workbench as prepared

    monkeypatch.setattr(prepared, "prepared_target", lambda *a, **k: (DENIED, False, False, "code"))
    monkeypatch.setattr(prepared, "prepared_tool_allowlist", lambda requested: requested)
    assert server._agent_turn("list files", tier="code") == BLOCKED % DENIED
    assert model_calls == []


def test_agent_turn_refuses_a_tier_bound_to_a_denied_model(catalog, model_calls, monkeypatch):
    monkeypatch.setitem(server.TIERS, "code", "Astra-Cyber-9B:latest")
    assert server._agent_turn("list files", tier="code") == BLOCKED % "Astra-Cyber-9B:latest"
    assert model_calls == []


def test_agent_turn_refuses_every_name_the_policy_adds(catalog, model_calls, policy_file):
    policy_file({"tool_lane_denied_models": {"names": [ALLOWED]}})
    assert server._agent_turn("list files", tier=ALLOWED) == BLOCKED % ALLOWED
    assert model_calls == []


def test_agent_turn_refuses_a_named_model_while_the_policy_is_unreadable(catalog, model_calls, policy_file):
    policy = policy_file("{not json")
    assert policy["error"]
    assert server._agent_turn("list files", tier=ALLOWED) == UNREADABLE % ALLOWED
    assert server._agent_turn("list files", tier=DENIED) == BLOCKED % DENIED  # still named as blocked
    assert model_calls == []
    assert server._serve_target(ALLOWED, None)[3] == "tool-lane-denied"
    # Chat by name still resolves.
    assert guard.chat_target(server._serve_target, ALLOWED, None)[0] == ALLOWED
    # A bound tier is not a caller-named model.
    assert guard.denied(server.TIERS["code"], "code", server._RUNTIME_POLICY) is False


def test_agent_turn_reaches_the_model_path_for_an_ordinary_model(catalog, model_calls):
    """The refusal is specific: an allowed model gets past it (and is stopped by our recorder)."""
    assert guard.resolved_refusal("code", ALLOWED, "code", server._RUNTIME_POLICY) == ""
    try:
        result = server._agent_turn("list files", tier="code")
    except Exception:  # noqa: BLE001 - the recorder raises once the lane reaches a model
        result = ""
    assert BLOCKED % ALLOWED not in str(result)
    assert model_calls, "an allowed model must proceed past the refusal to a model call"


# ---- the chat code gate ------------------------------------------------------

@pytest.mark.parametrize("name", DENIED_SPELLINGS)
def test_code_gate_never_runs_a_denied_models_reply(catalog, code_runs, name):
    assert run_gate(name, "model:" + name) == (RUNNABLE, None, False, {})
    # A model pin keeps the route's tier label; the list still applies.
    assert run_gate(name, "general") == (RUNNABLE, None, False, {})
    assert code_runs == []


def test_code_gate_runs_an_allowed_models_reply(catalog, code_runs):
    """The control: the same reply from an allowed model reaches the runner."""
    run_gate(ALLOWED, "model:" + ALLOWED)
    assert len(code_runs) == 1
    run_gate(ALLOWED, "general")
    assert len(code_runs) == 2


def test_code_gate_skips_a_model_added_by_the_policy(catalog, code_runs, policy_file):
    policy_file({"tool_lane_denied_models": {"names": [ALLOWED]}})
    assert run_gate(ALLOWED, "model:" + ALLOWED) == (RUNNABLE, None, False, {})
    assert code_runs == []


def chat_environment(monkeypatch, reply=RUNNABLE):
    # Escalation stays at its default: every caller here names its tier or
    # model, which never escalates; the default route's ladder is covered in
    # tests/test_tool_lane_default_route.py with the real escalation.
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(server, "_answer", lambda *a, **k: (reply, "iid-1", None))


@pytest.mark.parametrize("name", DENIED_SPELLINGS)
def test_sonder_by_tier_name_answers_without_running_code(catalog, code_runs, monkeypatch, name):
    chat_environment(monkeypatch)
    out = server._sonder_impl("write it", tier=name, session="none", project="none")
    assert "def double" in out and "blocked from tool-using" not in out
    assert code_runs == []


@pytest.mark.parametrize("name", [DENIED, HF_ORNITH])
def test_sonder_with_model_override_answers_without_running_code(catalog, code_runs, monkeypatch, name):
    chat_environment(monkeypatch)
    monkeypatch.setattr(server, "_resolve_model_pin", lambda selector: SimpleNamespace(ok=True, model=selector))
    out = server.sonder("write it", model_override=name, session="none", project="none")
    assert "def double" in out and "blocked from tool-using" not in out
    assert code_runs == []


def test_sonder_by_allowed_model_still_runs_the_gate(catalog, code_runs, monkeypatch):
    """The control that lets the two tests above fail."""
    chat_environment(monkeypatch)
    out = server._sonder_impl("write it", tier=ALLOWED, session="none", project="none")
    assert "def double" in out
    assert len(code_runs) == 1


def test_sonder_by_name_while_the_policy_is_unreadable_still_answers(catalog, code_runs, policy_file, monkeypatch):
    policy_file("{not json")
    chat_environment(monkeypatch)
    out = server._sonder_impl("write it", tier=ALLOWED, session="none", project="none")
    assert "def double" in out and "cannot be used in tool-using" not in out
    assert code_runs == []  # the unreadable list also withholds execution


# ---- text-only ensemble and consult ------------------------------------------

@pytest.fixture
def ensemble_calls(catalog, code_runs, monkeypatch):
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    asked = []

    def make(tier, model, *args, **kwargs):
        def generate(prompt, history=None, **kw):
            asked.append(model)
            return RUNNABLE

        return generate

    monkeypatch.setattr(server, "_make_tier_generate", make)
    return asked


def test_ensemble_targets_accept_a_named_denied_model(ensemble_calls):
    assert server._ensemble_targets(DENIED) == ([(DENIED, DENIED)], [])
    targets, unavailable = server._ensemble_targets(f"{HF_QWEN},{ALLOWED}")
    assert unavailable == []
    assert [model for _label, model in targets] == [HF_QWEN, ALLOWED]


def test_ensemble_answer_asks_a_named_denied_model_and_executes_nothing(ensemble_calls, code_runs):
    out = server.ensemble_answer("q", tiers=DENIED)
    assert ensemble_calls == [DENIED]
    assert "def double" in out and "blocked from tool-using" not in out
    ensemble_calls.clear()
    out = server.ensemble_answer("q", tiers=f"{DENIED},{ALLOWED}", synth_tier=DENIED)
    assert ensemble_calls == [DENIED, ALLOWED, DENIED], ensemble_calls
    assert "blocked from tool-using" not in out
    assert code_runs == []


def test_consult_asks_a_named_denied_model_and_executes_nothing(ensemble_calls, code_runs):
    out = server.consult("q", tiers=f"{DENIED},{ALLOWED}")
    assert DENIED in ensemble_calls and ALLOWED in ensemble_calls
    assert "blocked from tool-using" not in out
    assert code_runs == []


def test_explicit_fanout_selectors_stay_usable(catalog, monkeypatch):
    assert server._bare_tagged_model_request(DENIED, "hello") == {
        "kind": "model", "model": DENIED, "prompt": "hello"}
    monkeypatch.setattr(server, "_fanout_declares_generative_capability", lambda record: True)
    monkeypatch.setattr(server.ollama_endpoint, "is_loopback", lambda base: True)
    assert server._fanout_synthesis_model(DENIED) == DENIED


# ---- the fan-out sweep -------------------------------------------------------

def test_fanout_sweep_skips_denied_models_and_keeps_the_rest(catalog, monkeypatch):
    models = [(DENIED, {}), (ALLOWED, {}), (HF_QWEN, {}), ("Library/Astra-Cyber-9B:latest", {})]
    monkeypatch.setattr(server, "discovered_model_records", lambda: models)
    monkeypatch.setattr(server.fanout_store, "get_model_health", lambda name: None)
    plan, error = server._fanout_plan("local")
    assert error is None
    assert plan["selected"] == [ALLOWED]
    assert plan["skipped"] == [
        {"model": name, "reason": "chat by name only (tool-lane deny list)"}
        for name, _record in models if name != ALLOWED
    ]


def test_fanout_sweep_skips_a_model_added_by_the_policy(catalog, monkeypatch, policy_file):
    policy_file({"tool_lane_denied_models": {"names": [ALLOWED]}})
    monkeypatch.setattr(server, "discovered_model_records", lambda: [(ALLOWED, {}), ("other-model:7b", {})])
    monkeypatch.setattr(server.fanout_store, "get_model_health", lambda name: None)
    plan, error = server._fanout_plan("local")
    assert error is None and plan["selected"] == ["other-model:7b"]
    assert [item["model"] for item in plan["skipped"]] == [ALLOWED]


def test_sweep_skip_reason():
    assert guard.sweep_skip_reason(DENIED) == guard.SWEEP_SKIP_REASON
    assert guard.sweep_skip_reason(ALLOWED) == ""
    assert guard.sweep_skip_reason("") == ""


# ---- lanes that execute or apply output refuse once, in plain text ----------

def lane_calls(tmp_path):
    files = json.dumps({"a.c": "one C file"})
    return {
        "campaign_generate_compile_execute_record": lambda tier: server.campaign_generate_compile_execute_record(
            total=2, languages="python", tier=tier),
        "campaign_repo_repair": lambda tier: server.campaign_repo_repair(total=2, tier=tier),
        "codegen_build_loop": lambda tier: server.codegen_build_loop(
            str(tmp_path), files, "build", tiers=tier),
        "improve_function": lambda tier: server.improve_function(__file__, "lane_calls", tier=tier),
        "game_generate_and_test": lambda tier: server.game_generate_and_test("demo", "arena", tier=tier),
        "game_generation_campaign": lambda tier: server.game_generation_campaign("demo", total=3, tier=tier),
    }


LANES = (
    "campaign_generate_compile_execute_record", "campaign_repo_repair", "codegen_build_loop",
    "improve_function", "game_generate_and_test",
    "game_generation_campaign",
)


@pytest.mark.parametrize("lane", LANES)
@pytest.mark.parametrize("name", [DENIED, "Astra-Cyber-9B:latest", HF_QWEN])
def test_each_executing_lane_refuses_once_with_plain_text_before_any_work(
        catalog, work_started, model_calls, tmp_path, lane, name):
    out = lane_calls(tmp_path)[lane](name)
    assert out == BLOCKED % name
    assert not out.startswith("ERROR")
    assert out.count("blocked from tool-using lanes") == 1
    assert work_started == [] and model_calls == []


@pytest.mark.parametrize("lane", LANES)
def test_each_executing_lane_refuses_a_denied_name_inside_a_list(catalog, work_started, model_calls, tmp_path, lane):
    assert lane_calls(tmp_path)[lane]("code, " + DENIED) == BLOCKED % DENIED
    assert work_started == [] and model_calls == []


@pytest.mark.parametrize("lane", LANES)
def test_each_executing_lane_refuses_a_tier_bound_to_a_denied_model(
        catalog, work_started, model_calls, tmp_path, monkeypatch, lane):
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    assert lane_calls(tmp_path)[lane]("code") == BLOCKED % DENIED
    assert work_started == [] and model_calls == []


def test_build_loops_without_explicit_tiers_judge_every_bound_local_tier(
        catalog, work_started, model_calls, tmp_path, monkeypatch):
    files = json.dumps({"a.c": "one C file"})
    monkeypatch.setattr(server, "_configured_local_tiers", lambda: ("fast", "code", "general"))
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    assert server.codegen_build_loop(str(tmp_path), files, "build") == BLOCKED % DENIED
    assert server.ensemble_codegen_build_loop(str(tmp_path), files, "build") == BLOCKED % DENIED
    assert work_started == [] and model_calls == []


def test_build_loop_without_explicit_tiers_passes_when_every_bound_tier_is_allowed(
        catalog, work_started, tmp_path, monkeypatch):
    """The control: with nothing blocked the loop gets past the refusal and asks for a model."""
    monkeypatch.setattr(server, "_configured_local_tiers", lambda: ("fast", "code", "general"))
    files = json.dumps({"a.c": "one C file"})
    with contextlib.suppress(RuntimeError):  # our recorder ends the run at the first generation
        server.codegen_build_loop(str(tmp_path), files, "build")
    assert "ensemble_answer" in work_started


@pytest.mark.parametrize("lane", LANES)
def test_each_executing_lane_refuses_a_name_the_policy_added(
        catalog, work_started, model_calls, tmp_path, policy_file, lane):
    policy_file({"tool_lane_denied_models": {"names": [ALLOWED]}})
    assert lane_calls(tmp_path)[lane](ALLOWED) == BLOCKED % ALLOWED
    assert work_started == [] and model_calls == []


@pytest.mark.parametrize("lane", LANES)
def test_each_executing_lane_refuses_a_named_model_while_the_policy_is_unreadable(
        catalog, work_started, model_calls, tmp_path, policy_file, lane):
    policy_file("{not json")
    assert lane_calls(tmp_path)[lane](ALLOWED) == UNREADABLE % ALLOWED
    assert work_started == [] and model_calls == []


def test_selector_refusal_for_selectors_that_name_no_blocked_model():
    bindings = server.TIERS
    for selector in ("code", "fast,code", ALLOWED, "no-such-tier-xyz", "", "sonder", "local", " , "):
        assert guard.selector_refusal(selector, bindings, server._RUNTIME_POLICY) == "", selector
    assert guard.selector_refusal("code, Astra-Cyber-27B", bindings, server._RUNTIME_POLICY) == (
        BLOCKED % "Astra-Cyber-27B")


# ---- SONDER_CODE_LOCAL -------------------------------------------------------

def test_a_denied_sonder_code_local_never_backs_the_default_route(catalog, monkeypatch):
    from sonder_runtime.domain.runtime_model_configuration import (
        RuntimeModelConfiguration,
    )

    configuration = RuntimeModelConfiguration.from_environment({"SONDER_CODE_LOCAL": DENIED})
    monkeypatch.setattr(server, "LOCAL_CODE_MODEL", configuration.local_code_model)
    monkeypatch.setattr(server, "_get", lambda *a, **k: {})  # no catalog: the fallback model answers
    monkeypatch.setitem(server.TIERS, "general", "")
    monkeypatch.setattr(runtime_policy, "route_tier", lambda lane, policy=None, fallback="code": "general")
    model = server.resolve_sonder_model(False)
    assert model == configuration.stable_alias and model != DENIED
    assert guard.denied(model, "sonder", server._RUNTIME_POLICY) is False


# ---- bridged tiers: judged by the served model -------------------------------

BRIDGES = (
    pytest.param("sonder-inference", "sonder_inference",
                 {"SONDER_INFERENCE_TIER_MODELS": "code=astra-cyber-9b"}, "astra-cyber-9b", id="sonder-inference-tier-map"),
    pytest.param("sonder-inference", "sonder_inference",
                 {"SONDER_INFERENCE_MODEL": "Astra-Cyber-27B:latest"}, "Astra-Cyber-27B:latest", id="sonder-inference-model"),
    pytest.param("openrouter", "openrouter",
                 {"SONDER_OPENROUTER_TIER_MODELS": "code=acme/astra-cyber-27b"}, "acme/astra-cyber-27b", id="openrouter"),
    pytest.param("openai_compatible", "openai_compatible",
                 {"SONDER_OPENAI_MODEL": DENIED}, DENIED, id="openai-compatible"),
)


@pytest.mark.parametrize("provider, label, env, served", BRIDGES)
def test_agent_turn_refuses_a_tier_whose_served_model_is_denied(catalog, model_calls, monkeypatch, provider, label, env, served):
    bridge(monkeypatch, provider, **env)
    expected = served_refusal("code", label, served)
    assert server._agent_turn("list files", tier="code") == expected
    assert model_calls == []
    assert guard.resolved_refusal("code", ALLOWED, "code", server._RUNTIME_POLICY) == expected
    assert guard.denied(ALLOWED, "code", server._RUNTIME_POLICY) is True
    assert guard.selector_refusal("code", server.TIERS, server._RUNTIME_POLICY) == expected
    assert guard.selector_refusal("fast,code", server.TIERS, server._RUNTIME_POLICY) == expected
    assert guard.selector_refusal("fast", server.TIERS, server._RUNTIME_POLICY) == ""  # still on Ollama


@pytest.mark.parametrize("provider, label, env, served", BRIDGES)
def test_codegen_build_loop_refuses_a_tier_whose_served_model_is_denied(
        catalog, work_started, model_calls, tmp_path, monkeypatch, provider, label, env, served):
    bridge(monkeypatch, provider, **env)
    expected = served_refusal("code", label, served)
    files = json.dumps({"a.c": "one C file"})
    assert server.codegen_build_loop(str(tmp_path), files, "build", tiers="code") == expected
    monkeypatch.setattr(server, "_configured_local_tiers", lambda: ("fast", "code", "general"))
    assert server.codegen_build_loop(str(tmp_path), files, "build") == expected
    assert server.ensemble_codegen_build_loop(str(tmp_path), files, "build") == expected
    assert work_started == [] and model_calls == []


@pytest.mark.parametrize("provider, label, env, served", BRIDGES)
def test_a_tier_whose_served_model_is_denied_still_answers_chat(
        catalog, code_runs, monkeypatch, provider, label, env, served):
    bridge(monkeypatch, provider, **env)
    chat_environment(monkeypatch)
    out = server._sonder_impl("write it", tier="code", session="none", project="none")
    assert "def double" in out and "blocked from tool-using" not in out and "cannot be determined" not in out
    assert code_runs == []
    assert server._serve_target("code", None)[3] == "code"  # chat resolves the tier itself


def test_a_bridged_tier_whose_served_model_is_allowed_passes(catalog, monkeypatch):
    bridge(monkeypatch, "sonder-inference", SONDER_INFERENCE_TIER_MODELS="code=" + ALLOWED)
    assert guard.resolved_refusal("code", ALLOWED, "code", server._RUNTIME_POLICY) == ""
    assert guard.selector_refusal("code", server.TIERS, server._RUNTIME_POLICY) == ""
    bridge(monkeypatch, "openai_compatible", SONDER_OPENAI_MODEL=ALLOWED)
    assert guard.selector_refusal("code", server.TIERS, server._RUNTIME_POLICY) == ""


def test_the_default_route_follows_a_bridged_chat_tier(catalog, work_started, monkeypatch):
    monkeypatch.setenv("SONDER_GENERAL_PROVIDER", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "general=astra-cyber-9b")
    monkeypatch.setattr(guard._policy, "route_tier", lambda lane, policy=None, fallback="code": "general")
    expected = served_refusal("general", "sonder_inference", "astra-cyber-9b")
    for selector in ("", "sonder", "local"):
        assert guard.selector_refusal(selector, server.TIERS, server._RUNTIME_POLICY) == expected, selector
    assert server.campaign_generate_compile_execute_record(total=1, languages="python", tier="") == expected
    assert work_started == []


@pytest.mark.parametrize("provider, label, env", [
    pytest.param("openai_compatible", "openai_compatible", {}, id="openai-without-a-model"),
])
def test_an_unknown_served_identity_is_refused_in_tool_lanes_but_chats(
        catalog, model_calls, code_runs, monkeypatch, provider, label, env):
    bridge(monkeypatch, provider, **env)
    expected = UNKNOWN_OPENAI % "code" if provider == "openai_compatible" else UNKNOWN % ("code", label)
    assert server._agent_turn("list files", tier="code") == expected
    assert guard.selector_refusal("code", server.TIERS, server._RUNTIME_POLICY) == expected
    assert model_calls == []
    chat_environment(monkeypatch)
    out = server._sonder_impl("write it", tier="code", session="none", project="none")
    assert "def double" in out and "cannot be determined" not in out
    assert code_runs == []


def test_an_unreadable_provider_configuration_is_refused_as_unknown(catalog, model_calls, monkeypatch):
    bridge(monkeypatch, "sonder-inference", SONDER_INFERENCE_TIER_MODELS="not-a-pair")
    expected = UNKNOWN % ("code", "a non-Ollama provider")
    assert guard.selector_refusal("code", server.TIERS, server._RUNTIME_POLICY) == expected
    assert server._agent_turn("list files", tier="code") == expected
    assert model_calls == []


def test_a_named_model_is_not_judged_by_the_provider_environment(catalog, model_calls, monkeypatch):
    """A caller-named model runs on Ollama: a bridged code tier must not block an ordinary named one."""
    bridge(monkeypatch, "sonder-inference", SONDER_INFERENCE_TIER_MODELS="code=astra-cyber-9b")
    assert guard.selector_refusal(ALLOWED, server.TIERS, server._RUNTIME_POLICY) == ""
    assert guard.denied(ALLOWED, "model:" + ALLOWED) is False


# ---- the autopilot helpers ---------------------------------------------------

def autopilot_run(tier):
    return {"tier": tier, "objective": "fix it", "plan": [], "failures": 0, "max_failures": 3}


def test_autopilot_planner_and_reviewer_refuse_a_tier_bound_to_a_denied_model(catalog, model_calls, monkeypatch):
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    # The reviewer path refreshes (and so rebinds) the tier map; keep the binding under test.
    monkeypatch.setattr(server, "_refresh_runtime_policy", lambda create=True: server._RUNTIME_POLICY)
    monkeypatch.setattr(server.runtime_policy, "route_tier", lambda lane, policy=None, fallback="code": "code")
    for role in ("planner", "reviewer"):
        with pytest.raises(RuntimeError) as caught:
            server._autopilot_json_model({"tier": "code"}, role, "plan it", lambda payload: None)
        assert str(caught.value) == BLOCKED % DENIED
    with pytest.raises(RuntimeError) as caught:
        server._autopilot_review_model(autopilot_run("code"), "issue")
    assert str(caught.value) == BLOCKED % DENIED
    assert model_calls == []


@pytest.mark.parametrize("provider, label, env, served", BRIDGES)
def test_autopilot_helpers_refuse_a_denied_served_model(catalog, model_calls, monkeypatch, provider, label, env, served):
    bridge(monkeypatch, provider, **env)
    monkeypatch.setattr(server.runtime_policy, "route_tier", lambda lane, policy=None, fallback="code": "code")
    expected = served_refusal("code", label, served)
    with pytest.raises(RuntimeError) as caught:
        server._autopilot_json_model({"tier": "code"}, "planner", "plan it", lambda payload: None)
    assert str(caught.value) == expected
    with pytest.raises(RuntimeError) as caught:
        server._autopilot_json_model({"tier": "code"}, "reviewer", "review it", lambda payload: None)
    assert str(caught.value) == expected
    with pytest.raises(RuntimeError) as caught:
        server._autopilot_review_model(autopilot_run("code"), "issue")
    assert str(caught.value) == expected
    assert model_calls == []


def test_autopilot_helpers_reach_the_model_for_an_ordinary_tier(catalog, model_calls, monkeypatch):
    monkeypatch.setattr(server.runtime_policy, "route_tier", lambda lane, policy=None, fallback="code": "code")
    with pytest.raises(RuntimeError, match="must not happen"):
        server._autopilot_json_model({"tier": "code"}, "planner", "plan it", lambda payload: None)
    assert model_calls  # got past the refusal and asked for a model


# ---- the parallel generate-and-run tools -------------------------------------

PARALLEL_TOOLS = {
    "parallel_generate_run": lambda tier: server.parallel_generate_run(
        "double a number", variants=2, max_workers=1, tier=tier),
    "parallel_generate_run_languages": lambda tier: server.parallel_generate_run_languages(
        "double a number", languages="python", max_workers=1, tier=tier),
}


@pytest.fixture
def parallel_work(monkeypatch):
    """The two parallel tools with generation and execution recorded, nothing real run."""
    seen = {"generated": [], "executed": []}

    def make_tier_generate(tier, *args, **kwargs):
        def gen(prompt):
            seen["generated"].append(tier)
            return RUNNABLE

        gen.last_usage = {}
        return gen

    def run_code(code, *args, **kwargs):
        seen["executed"].append(code)
        return True, "ok"

    monkeypatch.setattr(server, "_make_tier_generate", make_tier_generate)
    monkeypatch.setattr(server.grounding, "run_code", run_code)
    monkeypatch.setattr(server.grounding, "run_language_code", run_code)
    return seen


@pytest.mark.parametrize("tool", PARALLEL_TOOLS)
def test_parallel_tools_refuse_a_tier_bound_to_a_denied_model(catalog, parallel_work, monkeypatch, tool):
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    out = PARALLEL_TOOLS[tool]("code")
    assert out == BLOCKED % DENIED
    assert not out.startswith("ERROR")
    assert parallel_work == {"generated": [], "executed": []}


@pytest.mark.parametrize("tool", PARALLEL_TOOLS)
@pytest.mark.parametrize("provider, label, env, served", BRIDGES)
def test_parallel_tools_refuse_a_tier_whose_served_model_is_denied(
        catalog, parallel_work, monkeypatch, tool, provider, label, env, served):
    bridge(monkeypatch, provider, **env)
    assert PARALLEL_TOOLS[tool]("code") == served_refusal("code", label, served)
    assert parallel_work == {"generated": [], "executed": []}


@pytest.mark.parametrize("tool", PARALLEL_TOOLS)
def test_parallel_tools_refuse_a_bridged_tier_with_an_undetermined_served_model(
        catalog, parallel_work, monkeypatch, tool):
    bridge(monkeypatch, "openai_compatible")
    assert PARALLEL_TOOLS[tool]("code") == UNKNOWN_OPENAI % "code"
    assert parallel_work == {"generated": [], "executed": []}


@pytest.mark.parametrize("tool", PARALLEL_TOOLS)
def test_parallel_tools_generate_and_run_for_an_ordinary_model(catalog, parallel_work, tool):
    """The control: an allowed tier gets past the refusal, generates and runs its candidates."""
    out = PARALLEL_TOOLS[tool]("code")
    assert "blocked from tool-using" not in out and "cannot be determined" not in out
    assert "[PASS]" in out
    assert parallel_work["generated"] and parallel_work["executed"]


@pytest.mark.parametrize("tool", PARALLEL_TOOLS)
def test_parallel_tools_keep_the_unknown_tier_and_cloud_messages(catalog, parallel_work, monkeypatch, tool):
    assert PARALLEL_TOOLS[tool]("no-such-tier").startswith("ERROR: unknown tier 'no-such-tier'")
    monkeypatch.setitem(server.TIERS, "cloud-code", "qwen3-coder:480b-cloud")
    monkeypatch.setattr(server, "_cloud_allowed_policy", lambda env: False)
    assert PARALLEL_TOOLS[tool]("cloud-code") == server._cloud_disabled_message()
    assert parallel_work == {"generated": [], "executed": []}


# ---- the self-modification job -----------------------------------------------

@pytest.fixture
def selfmod_job(monkeypatch):
    """``nightly_selfmod`` with every model entry recorded; the catalog lists every name."""
    from scripts import nightly_selfmod

    asked = []
    reply = "def f():\n    return 1\n"

    def make_generate(model, *args, **kwargs):
        asked.append(("_make_generate", model))
        return lambda prompt: reply

    def ensemble_answer(prompt, **kwargs):
        asked.append(("ensemble_answer", kwargs.get("tiers")))
        return reply

    monkeypatch.setattr(server, "_make_generate", make_generate)
    monkeypatch.setattr(server, "ensemble_answer", ensemble_answer)
    monkeypatch.setattr(nightly_selfmod, "_local_catalog_model", lambda srv, selected: selected)
    # ``RuntimeError`` is the base of ``ModelRefused``: on a build without the
    # refusal the check then fails as "did not raise", not as a missing name.
    refused = getattr(nightly_selfmod, "ModelRefused", RuntimeError)
    return SimpleNamespace(module=nightly_selfmod, asked=asked, reply=reply.strip(), refused=refused)


@pytest.mark.parametrize("name", [DENIED, "Astra-Cyber-9B:latest", HF_QWEN])
def test_selfmod_ask_refuses_an_explicit_denied_model_before_any_call(catalog, selfmod_job, name):
    with pytest.raises(selfmod_job.refused) as caught:
        selfmod_job.module._ask(server, "improve f", model=name)
    assert type(caught.value).__name__ == "ModelRefused"
    assert str(caught.value) == "model refused: " + BLOCKED % name
    assert selfmod_job.asked == []


def test_selfmod_ask_refuses_a_model_the_policy_added(catalog, selfmod_job, policy_file):
    policy_file({"tool_lane_denied_models": {"names": [ALLOWED]}})
    with pytest.raises(selfmod_job.refused):
        selfmod_job.module._ask(server, "improve f", model=ALLOWED)
    assert selfmod_job.asked == []


@pytest.mark.parametrize("provider, label, env, served", BRIDGES)
def test_selfmod_ask_refuses_a_code_tier_whose_served_model_is_denied(
        catalog, selfmod_job, monkeypatch, provider, label, env, served):
    bridge(monkeypatch, provider, **env)
    with pytest.raises(selfmod_job.refused) as caught:
        selfmod_job.module._ask(server, "improve f")
    assert str(caught.value) == "model refused: " + served_refusal("code", label, served)
    assert selfmod_job.asked == []


def test_selfmod_ask_refuses_a_code_tier_bound_to_a_denied_model(catalog, selfmod_job, monkeypatch):
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    with pytest.raises(selfmod_job.refused):
        selfmod_job.module._ask(server, "improve f")
    assert selfmod_job.asked == []


def test_selfmod_ask_returns_an_ordinary_models_reply(catalog, selfmod_job):
    """The control: both branches still ask and return the reply."""
    assert selfmod_job.module._ask(server, "improve f", model=ALLOWED) == selfmod_job.reply
    assert selfmod_job.module._ask(server, "improve f") == selfmod_job.reply
    assert selfmod_job.asked == [("_make_generate", ALLOWED), ("ensemble_answer", "code")]


def test_selfmod_run_stops_before_any_run_for_a_denied_model(catalog, selfmod_job, monkeypatch):
    module = selfmod_job.module
    touched = []
    for name in ("_isolation_refusal", "_compose_stage_journal", "propose_objective"):
        monkeypatch.setattr(module, name, lambda *a, _n=name, **k: touched.append(_n))
    monkeypatch.setattr(module.selfmod, "settings", lambda: {"enabled": True, "mode": "propose"})
    monkeypatch.setattr(module.selfmod, "_git_info", lambda repo: touched.append("_git_info") or (True, "", ""))
    monkeypatch.setattr(module.selfmod, "create_plan", lambda *a, **k: touched.append("create_plan"))
    out = module.run(server, lambda message: None, model=DENIED)
    assert out == "model refused, no run started: " + BLOCKED % DENIED
    assert touched == [] and selfmod_job.asked == []


def test_selfmod_forever_stops_on_a_model_refusal(selfmod_job, monkeypatch):
    from scripts import selfmod_forever

    passes, lines = [], []
    monkeypatch.setattr(selfmod_forever.nightly_selfmod, "reclaim_orphans", lambda log: None)
    monkeypatch.setattr(selfmod_forever.nightly_selfmod, "run",
                        lambda *a, **k: passes.append(1) or "model refused, no run started: blocked")
    monkeypatch.setattr(selfmod_forever.selfmod, "settings", lambda: {"mode": "propose"})
    # A bounded barren limit: a loop that ignored the refusal would retry up to it, not forever.
    monkeypatch.setattr("sys.argv", ["selfmod_forever", "--hours", "1", "--max-barren", "3", "--model", DENIED])
    monkeypatch.setattr("builtins.print", lambda *a, **k: lines.append(" ".join(map(str, a))))
    assert selfmod_forever.main() == 0
    assert passes == [1]  # one refused pass, then the loop stops instead of retrying
    assert any("may not write code" in line for line in lines)


def test_the_nightly_stage_reports_a_model_refusal_as_a_failure():
    from scripts import nightly_self_improve

    assert nightly_self_improve._blocking_result("selfmod", "model refused, no run started: x") == (
        "model refused, no run started: x")
    assert nightly_self_improve._blocking_result("selfmod", "no objective proposed") is None


# ---- mesh_generate -------------------------------------------------------------

class _MeshRunner:
    @staticmethod
    def locate_blender():
        return "blender"


def test_mesh_generate_refuses_a_denied_tier_before_any_generation(catalog, model_calls, monkeypatch, tmp_path):
    from sonder_runtime.bootstrap import mesh_agent_tools
    from sonder_runtime.domain.common.errors import SonderError

    generated = []
    monkeypatch.setattr(server, "_generate_text", lambda *a, **k: generated.append(a) or "")
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    with pytest.raises(SonderError) as caught:
        mesh_agent_tools.generate_mesh("a crate", tiers="general,code", output_dir=str(tmp_path),
                                       rt=server, runner=_MeshRunner)
    assert str(caught.value) == BLOCKED % DENIED
    monkeypatch.setitem(server.TIERS, "code", ALLOWED)
    bridge(monkeypatch, "sonder-inference", SONDER_INFERENCE_TIER_MODELS="code=" + DENIED)
    with pytest.raises(SonderError) as caught:
        mesh_agent_tools.generate_mesh("a crate", tiers="code", output_dir=str(tmp_path),
                                       rt=server, runner=_MeshRunner)
    assert str(caught.value) == served_refusal("code", "sonder_inference", DENIED)
    assert generated == [] and model_calls == []


@pytest.mark.parametrize("vision_model", [DENIED, ALLOWED])
def test_mesh_vision_critique_is_refused_for_a_denied_vision_tier(catalog, monkeypatch, tmp_path, vision_model):
    """The critique steers the next round's script: a denied vision tier turns it off, nothing else."""
    from sonder_runtime.bootstrap import mesh_agent_tools

    asked = []
    monkeypatch.setitem(server.TIERS, "vision", vision_model)
    monkeypatch.setattr(server, "_vision_analyze_impl", lambda *a, **k: asked.append(a) or "looks fine")
    critiques = []

    def run_loop(request, *, critique, **_kwargs):
        critiques.append(critique(str(tmp_path / "sheet.png")))
        return {"ok": False, "within_budget": True, "vision": "on", "elapsed_s": 0, "candidates": []}

    monkeypatch.setattr(mesh_agent_tools, "run_loop", run_loop)
    monkeypatch.setattr(server.assetgen, "workspace_root", lambda: str(tmp_path))
    report = mesh_agent_tools.generate_mesh("a crate", tiers="general", output_dir=str(tmp_path),
                                            rt=server, runner=_MeshRunner)
    if vision_model == DENIED:
        assert critiques == [None] and asked == []
        assert report["vision"] == "unavailable: " + BLOCKED % DENIED
    else:
        assert critiques == ["looks fine"] and len(asked) == 1
        assert report["vision"] == "on"
