"""The default route's escalation ladder under the tool-lane deny list.

A lane that runs the default route's reply (``tier`` ``""``, ``sonder`` or
``local``) cannot know which rung will answer: ``sonder()`` starts on the
policy's chat tier and, with ``SONDER_MODEL_ESCALATION`` at its default (on),
steps up over the configured local tiers when an answer is empty or its code
fails.  These tests run the real ``sonder()`` escalation, bind and code gate;
only the model answer (``server._answer``) and the execution sinks are faked.
A bridged ladder tier serving a denied model must refuse the lane once, before
any model call or execution; with ordinary models the lane still escalates and
still runs the result.  Nothing leaves the process.
"""
from __future__ import annotations

import inspect
import re

import pytest

import game_forge
import grounding
import server
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters import tool_lane_guard as guard
from sonder_runtime.application.chat import provider_bridge
from tests.support.tool_lane import (
    ALLOWED,
    BLOCKED,
    DENIED,
    ESCALATION,
    RUNNABLE,
    SERVED,
    UNKNOWN,
    UNKNOWN_OPENAI,
    clear_environment,
)

OTHER = "qwen2.5-coder:7b"
DEFAULT_SELECTORS = ("", "sonder", "local")
TASK = {"name": "t", "prompt": "write a python function that doubles a number", "check": "assert True"}


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.delenv("SONDER_MODEL_ESCALATION", raising=False)  # escalation at its default: on
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None, raising=False)
    before = dict(server.TIERS)
    monkeypatch.setattr(server, "_RUNTIME_POLICY", server._RUNTIME_POLICY)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    server._refresh_runtime_policy(create=False)
    server.TIERS["fast"] = ALLOWED
    server.TIERS["general"] = ALLOWED
    server.TIERS["code"] = OTHER
    monkeypatch.setattr(server.web_tools, "enabled", lambda: False)
    monkeypatch.setattr(server, "_code_gate_enabled", lambda: False)
    yield
    server.TIERS.clear()
    server.TIERS.update(before)


def bridge_code(monkeypatch, served=DENIED):
    """Serve the ``code`` ladder tier through Sonder Inference with ``served``."""
    monkeypatch.setenv("SONDER_CODE_PROVIDER", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "code=" + served)


CODE_REFUSAL = ESCALATION % ("code", SERVED % ("code", "sonder_inference", DENIED), "code")


@pytest.fixture
def answered(monkeypatch):
    """The model answer, faked: the chat tier answers empty, any other rung answers code."""
    calls = []

    def fake_answer(conn, prompt, model, *args, **kwargs):
        rung = provider_bridge.active_rung()
        calls.append((kwargs.get("tier"), model, getattr(rung, "provider", None)))
        return ("" if model == ALLOWED else RUNNABLE), None, None

    monkeypatch.setattr(server, "_answer", fake_answer)
    return calls


@pytest.fixture
def executed(monkeypatch):
    """Every execution sink a default-route lane can reach, recorded."""
    runs = []

    def run_generated_code(runner, code, **kwargs):
        runs.append(code)
        return True, "ok"

    def run_code(code, *args, **kwargs):
        runs.append(code)
        return True, "ok"

    import curriculum_run
    import eval_retrieval
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl

    monkeypatch.setattr(server._powershell_gate, "run_generated_code", run_generated_code)
    # Each consumer's own ``grounding`` (another test may have reloaded the module).
    for module in {id(m): m for m in (grounding, server.grounding, serve.grounding, repl.grounding,
                                      curriculum_run.grounding, eval_retrieval.grounding)}.values():
        monkeypatch.setattr(module, "run_code", run_code)
    monkeypatch.setattr(game_forge, "run_project", lambda project, code, timeout: runs.append(code) or {
        "ok": True, "output": "GAME_OK"})
    return runs


@pytest.fixture
def game_project(monkeypatch, tmp_path):
    """A game project that needs no asset pack on disk; the generated code is what runs."""
    project = {
        "name": "demo", "language": "python", "dimension": "2d", "root": str(tmp_path),
        "source": str(tmp_path / "game.py"), "frame": str(tmp_path / "frame.ppm"),
    }
    monkeypatch.setattr(game_forge, "prepare_project", lambda *a, **k: dict(project))
    monkeypatch.setattr(game_forge, "generation_prompt", lambda project, concept="": "Write a complete runnable python program for this game.")
    monkeypatch.setattr(game_forge, "reference_source", lambda *a, **k: (_ for _ in ()).throw(ValueError("none")))
    monkeypatch.setattr(game_forge, "autofix_standard_library", lambda code, language: code)
    monkeypatch.setattr(game_forge, "validate_in_house", lambda code, language: [])
    monkeypatch.setattr(game_forge, "contract_issues", lambda code, language: [])
    return project


def campaign(tier):
    return server.campaign_generate_compile_execute_record(
        total=1, languages="python", tier=tier, max_workers=1, repair_rounds=0, record_failures=False)


def train_http():
    from sonder_runtime.interfaces.http import serve

    return serve._do_train(1)


def train_repl(capsys):
    from sonder_runtime.interfaces.repl import repl

    repl._run_train(1)
    return capsys.readouterr().out


def game(tier):
    return server.game_generate_and_test("demo", "arena", tier=tier, repair_rounds=0)


@pytest.fixture
def one_task(monkeypatch):
    from sonder_runtime.interfaces.http import serve
    from sonder_runtime.interfaces.repl import repl

    monkeypatch.setattr(serve.training_tasks, "sample", lambda n: [dict(TASK)])
    monkeypatch.setattr(repl.training_tasks, "sample", lambda n: [dict(TASK)])


# ---- the default route refuses a denied rung it can step up to -----------------

@pytest.mark.parametrize("tier", DEFAULT_SELECTORS)
def test_campaign_refuses_a_ladder_tier_whose_served_model_is_denied(monkeypatch, answered, executed, tier):
    bridge_code(monkeypatch)
    assert campaign(tier) == CODE_REFUSAL
    assert answered == [] and executed == []


@pytest.mark.parametrize("tier", ("", "sonder"))
def test_game_generation_refuses_a_ladder_tier_whose_served_model_is_denied(
        monkeypatch, answered, executed, game_project, tier):
    bridge_code(monkeypatch)
    assert game(tier) == CODE_REFUSAL
    assert answered == [] and executed == []


def test_http_train_refuses_a_ladder_tier_whose_served_model_is_denied(monkeypatch, answered, executed, one_task):
    bridge_code(monkeypatch)
    assert train_http() == CODE_REFUSAL
    assert answered == [] and executed == []


def test_repl_train_refuses_a_ladder_tier_whose_served_model_is_denied(
        monkeypatch, capsys, answered, executed, one_task):
    bridge_code(monkeypatch)
    out = train_repl(capsys)
    assert CODE_REFUSAL in out and out.count("blocked from tool-using lanes") == 1
    assert answered == [] and executed == []


def test_curriculum_practice_refuses_a_ladder_tier_whose_served_model_is_denied(
        monkeypatch, capsys, answered, executed):
    import curriculum_run

    bridge_code(monkeypatch)
    assert curriculum_run.practice_on([dict(TASK)]) == (0, 0)
    assert CODE_REFUSAL in capsys.readouterr().out
    assert answered == [] and executed == []


def test_retrieval_eval_refuses_a_ladder_tier_whose_served_model_is_denied(monkeypatch, answered, executed):
    import eval_retrieval

    bridge_code(monkeypatch)
    with pytest.raises(eval_retrieval.ModelRefused) as caught:
        eval_retrieval.run_task(dict(TASK))
    assert str(caught.value) == CODE_REFUSAL
    assert answered == [] and executed == []


# ---- with ordinary models nothing changes ---------------------------------------

def test_campaign_still_escalates_and_runs_the_result_for_ordinary_models(monkeypatch, answered, executed):
    bridge_code(monkeypatch, served=OTHER)
    out = campaign("sonder")
    assert [tier for tier, _model, _provider in answered] == ["general", "code"], answered
    assert answered[1][2] == "sonder_inference"
    assert len(executed) == 1 and "def double" in executed[0]
    assert "blocked from tool-using" not in out and "cannot be determined" not in out


def test_train_still_escalates_and_runs_the_result_for_ordinary_models(monkeypatch, answered, executed, one_task):
    out = train_http()
    assert [tier for tier, _model, _provider in answered] == ["general", "code"], answered
    assert len(executed) == 1 and "def double" in executed[0]
    assert "blocked from tool-using" not in out


def test_game_generation_still_escalates_and_runs_the_result_for_ordinary_models(
        monkeypatch, answered, executed, game_project):
    game("sonder")
    assert [tier for tier, _model, _provider in answered] == ["general", "code"], answered
    assert len(executed) == 1 and "def double" in executed[0]


# ---- the guard: every tier the default route can reach ----------------------------

def policy_with(monkeypatch, **local_models):
    """Bind extra local tiers in the policy and the live tier map."""
    policy = dict(server._RUNTIME_POLICY)
    policy["local_models"] = dict(policy["local_models"], **local_models)
    for tier, model in local_models.items():
        monkeypatch.setitem(server.TIERS, tier, model)
    return policy


def test_the_default_route_judges_every_configured_local_tier(monkeypatch):
    reachable = runtime_policy.configured_tiers(server._RUNTIME_POLICY, server.TIERS)
    assert reachable == server._configured_local_tiers()  # the escalation plan's own set
    assert {"fast", "general", "code"} <= set(reachable)
    for selector in DEFAULT_SELECTORS:
        assert guard.selector_refusal(selector, server.TIERS, server._RUNTIME_POLICY) == ""
    bridge_code(monkeypatch)
    for selector in DEFAULT_SELECTORS + (" , ", "fast,sonder"):
        assert guard.selector_refusal(selector, server.TIERS, server._RUNTIME_POLICY) == CODE_REFUSAL, selector
    assert guard.default_route_refusal(server.TIERS, server._RUNTIME_POLICY) == CODE_REFUSAL
    assert guard.selector_refusal("fast", server.TIERS, server._RUNTIME_POLICY) == ""  # an explicit tier is itself


def test_the_optional_reasoning_rung_is_judged(monkeypatch):
    policy = policy_with(monkeypatch, reasoning=OTHER)
    monkeypatch.setenv("SONDER_REASONING_PROVIDER", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "reasoning=" + DENIED)
    assert "reasoning" in runtime_policy.configured_tiers(policy, server.TIERS)
    expected = ESCALATION % ("reasoning", SERVED % ("reasoning", "sonder_inference", DENIED), "reasoning")
    assert guard.selector_refusal("sonder", server.TIERS, policy) == expected


def test_an_unbound_tier_is_not_a_rung_and_is_not_judged(monkeypatch):
    """A tier the policy leaves unset never answers the default route, so it cannot refuse it."""
    assert "reasoning" not in runtime_policy.configured_tiers(server._RUNTIME_POLICY, server.TIERS)
    monkeypatch.setenv("SONDER_REASONING_PROVIDER", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "reasoning=" + DENIED)
    assert guard.selector_refusal("", server.TIERS, server._RUNTIME_POLICY) == ""


def test_a_ladder_tier_bound_to_a_denied_name_is_judged(monkeypatch):
    monkeypatch.setitem(server.TIERS, "code", DENIED)
    expected = ESCALATION % ("code", BLOCKED % DENIED, "code")
    assert guard.selector_refusal("", server.TIERS, server._RUNTIME_POLICY) == expected


def test_a_ladder_tier_with_an_undetermined_served_model_is_refused(monkeypatch):
    monkeypatch.setenv("SONDER_CODE_PROVIDER", "openai_compatible")
    expected = ESCALATION % ("code", UNKNOWN_OPENAI % "code", "code")
    assert guard.selector_refusal("local", server.TIERS, server._RUNTIME_POLICY) == expected
    monkeypatch.setenv("SONDER_CODE_PROVIDER", "sonder-inference")
    expected = ESCALATION % ("code", UNKNOWN % ("code", "sonder_inference"), "code")
    assert guard.selector_refusal("local", server.TIERS, server._RUNTIME_POLICY) == expected


def test_the_chat_tier_keeps_its_own_refusal(monkeypatch):
    monkeypatch.setenv("SONDER_GENERAL_PROVIDER", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "general=%s,code=%s" % (DENIED, DENIED))
    expected = SERVED % ("general", "sonder_inference", DENIED)
    assert guard.selector_refusal("", server.TIERS, server._RUNTIME_POLICY) == expected


def test_the_knob_off_does_not_widen_what_an_explicit_tier_judges(monkeypatch):
    """The default-route check is a function of the configuration, not of the escalation knob."""
    bridge_code(monkeypatch)
    monkeypatch.setenv("SONDER_MODEL_ESCALATION", "0")
    assert guard.selector_refusal("sonder", server.TIERS, server._RUNTIME_POLICY) == CODE_REFUSAL
    assert guard.selector_refusal("general", server.TIERS, server._RUNTIME_POLICY) == ""


# ---- a prompt that names its own model (``natural_model_request``) ---------------

NAMED = "use model %s: write a python function that doubles a number" % DENIED


@pytest.fixture
def denied_in_catalog(monkeypatch):
    def record(selector):
        key = str(selector or "").strip().casefold()
        return (DENIED, {"name": DENIED}) if key == DENIED else None

    monkeypatch.setattr(server, "resolve_discovered_model_record", record)
    monkeypatch.setattr(server, "_fanout_nonchat_reason", lambda record: "")


def test_sonder_re_routes_a_prompt_that_names_a_model(denied_in_catalog):
    """What the consumers below guard against: the prompt text overrides tier and model_override."""
    request = server.natural_model_request(NAMED)
    assert request["kind"] == "model" and request["model"] == DENIED
    assert guard.prompt_refusal(NAMED, server.natural_model_request, server.TIERS) == BLOCKED % DENIED
    assert guard.prompt_refusal(TASK["prompt"], server.natural_model_request, server.TIERS) == ""
    ensemble = "ask code and reasoning models: write it"
    assert server.natural_model_request(ensemble)["kind"] == "ensemble"
    assert guard.prompt_refusal(ensemble, server.natural_model_request, server.TIERS) == ""


def test_curriculum_practice_refuses_a_task_whose_text_names_a_denied_model(
        denied_in_catalog, answered, executed, capsys):
    import curriculum_run

    assert curriculum_run.practice_on([dict(TASK, prompt=NAMED)]) == (0, 0)
    assert BLOCKED % DENIED in capsys.readouterr().out
    assert answered == [] and executed == []


def test_retrieval_eval_refuses_a_task_whose_text_names_a_denied_model(denied_in_catalog, answered, executed):
    import eval_retrieval

    with pytest.raises(eval_retrieval.ModelRefused, match=re.escape(BLOCKED % DENIED)):
        eval_retrieval.run_task(dict(TASK, prompt=NAMED))
    with pytest.raises(eval_retrieval.ModelRefused, match=re.escape(BLOCKED % DENIED)):
        eval_retrieval.run_task(dict(TASK, prompt=NAMED), model=ALLOWED)
    assert answered == [] and executed == []


def test_curriculum_practice_still_follows_a_task_that_names_an_ordinary_model(monkeypatch, answered, executed):
    import curriculum_run

    monkeypatch.setattr(server, "resolve_discovered_model_record",
                        lambda selector: (OTHER, {"name": OTHER}) if selector == OTHER else None)
    monkeypatch.setattr(server, "_fanout_nonchat_reason", lambda record: "")
    curriculum_run.practice_on([dict(TASK, prompt="use model %s: write it" % OTHER)])
    assert [model for _tier, model, _provider in answered] == [OTHER]
    assert len(executed) == 1


def _no_model_request(prompt):
    return server.natural_model_request(prompt) is None and not prompt.lstrip().startswith("/")


def test_lanes_that_write_their_own_prompts_cannot_name_a_model():
    """The other default-route consumers build every prompt from fixed text, so none re-routes."""
    import game_ladder
    import solver
    import training_tasks

    for task_name, task_text in server._CAMPAIGN_TASKS:
        for language in ("python", "javascript", "powershell", "cpp", "csharp"):
            assert _no_model_request(server._campaign_prompt(language, task_name, task_text, "note")), task_name
    for task in training_tasks.TASKS:
        assert _no_model_request(task["prompt"]), task["name"]
    for level in game_ladder.LEVELS:
        assert _no_model_request(level["prompt"]), level["name"]
        assert _no_model_request(solver._repair_prompt(level["prompt"], "code", "error")), level["name"]
    project = {"name": "demo", "language": "python", "dimension": "2d", "root": "r", "source": "s",
               "frame": "f", "files": [], "assets": []}
    game_text = game_forge.generation_prompt(project, "use model %s: write it" % DENIED)  # the concept is mid-prompt
    assert game_text.startswith("Create a compact greenfield") and _no_model_request(game_text)
    repair = re.search(r'prompt = \(\s*"([^"]+)"', inspect.getsource(server.campaign_repo_repair))
    assert repair and repair.group(1).startswith("A small Python project")
    assert _no_model_request(repair.group(1) + "rest of the prompt")
