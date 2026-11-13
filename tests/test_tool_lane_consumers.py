"""Consumers that run model output refuse a tool-lane-denied model before asking it.

These lanes reach the model through a text entry point (``sonder``,
``ensemble_answer``) or a raw endpoint, so the bottom check does not cover
them: each must refuse first.  Every refusal is asserted to have asked the
model nothing; every permitted route to have reached the model.  Nothing
leaves the process.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import server
from sonder_runtime.adapters import tool_lane_guard as guard
from tests.support.tool_lane import ALLOWED, DENIED, clear_environment

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "examples" / "codegen-arena-shooter"
TIER_NAMES = ("fast", "code", "general", "reasoning")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    clear_environment(monkeypatch)
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    monkeypatch.setattr(guard, "_GENERATION_CACHE", None, raising=False)
    for tier in TIER_NAMES:
        monkeypatch.setitem(server.TIERS, tier, ALLOWED)


def deny_default_route(monkeypatch):
    """Serve the chat tier the default route reaches through a bridge, with a denied model.

    Tier bindings never hold a denied model (the policy loader drops it), so a
    bridged provider is how the default route reaches one.
    """
    monkeypatch.setenv("SONDER_GENERAL_PROVIDER", "sonder-inference")
    monkeypatch.setenv("SONDER_INFERENCE_TIER_MODELS", "general=" + DENIED)
    monkeypatch.setattr(guard._policy, "route_tier", lambda lane, policy=None, fallback="code": "general")


class Asked:
    """Records each model call and answers with no code (nothing to run)."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return "no code here"


def fake_runtime(model=ALLOWED):
    asked = Asked()
    runtime = SimpleNamespace(
        TIERS={tier: model for tier in TIER_NAMES},
        _tool_lane_guard=guard,
        sonder=asked,
        ensemble_answer=asked,
        parse_interaction_id=lambda _text: "",
        record_outcome=lambda *_a: "",
    )
    return runtime, asked


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


TASK = {"name": "t", "prompt": "write f", "check": "assert True"}


# -- practice (/train) ---------------------------------------------------------

@pytest.mark.parametrize("denied", [True, False])
def test_http_train_refuses_a_denied_default_route(monkeypatch, denied):
    from sonder_runtime.interfaces.http import serve

    if denied:
        deny_default_route(monkeypatch)
    runtime, asked = fake_runtime()
    monkeypatch.setattr(serve, "server", runtime)
    monkeypatch.setattr(serve.training_tasks, "sample", lambda n: [dict(TASK)])
    out = serve._do_train(1)
    if denied:
        assert DENIED in out and asked.calls == []
    else:
        assert len(asked.calls) == 1


@pytest.mark.parametrize("denied", [True, False])
def test_repl_train_refuses_a_denied_default_route(monkeypatch, capsys, denied):
    from sonder_runtime.interfaces.repl import repl

    if denied:
        deny_default_route(monkeypatch)
    runtime, asked = fake_runtime()
    monkeypatch.setattr(repl, "server", runtime)
    monkeypatch.setattr(repl.training_tasks, "sample", lambda n: [dict(TASK)])
    repl._run_train(1)
    if denied:
        assert DENIED in capsys.readouterr().out and asked.calls == []
    else:
        assert len(asked.calls) == 1


# -- curriculum, solver, harness, retrieval eval ---------------------------------

@pytest.mark.parametrize("denied", [True, False])
def test_curriculum_practice_refuses_a_denied_default_route(monkeypatch, capsys, denied):
    import curriculum_run

    if denied:
        deny_default_route(monkeypatch)
    asked = Asked()
    monkeypatch.setattr(server, "sonder", asked)
    passed, lessons = curriculum_run.practice_on([dict(TASK)])
    if denied:
        assert (passed, lessons) == (0, 0) and asked.calls == []
        assert DENIED in capsys.readouterr().out
    else:
        assert len(asked.calls) == 1


def test_curriculum_generator_refuses_a_denied_model(monkeypatch):
    import curriculum_run

    made = []
    monkeypatch.setattr(server, "resolve_sonder_model", lambda _cloud: DENIED)
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: made.append(a))
    with pytest.raises(SystemExit, match=DENIED):
        curriculum_run.build_gen_fn()
    assert made == []


def test_solver_refuses_a_denied_model(monkeypatch):
    import eval_solver

    made = []
    monkeypatch.setattr(server, "resolve_sonder_model", lambda _cloud: DENIED)
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: made.append(a))
    with pytest.raises(SystemExit, match=DENIED):
        eval_solver._gen_fn()
    assert made == []


@pytest.mark.parametrize("model", [DENIED, "Astra-Cyber-27B:latest"])
def test_harness_provider_refuses_a_denied_model(monkeypatch, model):
    import eval_harness

    made = []
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: made.append(a))
    with pytest.raises(eval_harness.HarnessError):
        eval_harness.OllamaProvider(model)
    assert made == []


def test_harness_provider_builds_for_an_ordinary_model(monkeypatch):
    import eval_harness

    made = []
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: made.append(a) or (lambda p: ""))
    provider = eval_harness.OllamaProvider(ALLOWED)
    assert provider.model == ALLOWED and len(made) == 1


def test_retrieval_eval_refuses_a_denied_model_before_either_condition(monkeypatch):
    import eval_retrieval

    asked = Asked()
    monkeypatch.setattr(server, "sonder", asked)
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: asked)
    with pytest.raises(eval_retrieval.ModelRefused, match=DENIED):
        eval_retrieval.run_task(dict(TASK), model=DENIED)
    assert asked.calls == []


def test_retrieval_eval_refuses_a_denied_default_route(monkeypatch):
    import eval_retrieval

    deny_default_route(monkeypatch)
    asked = Asked()
    monkeypatch.setattr(server, "sonder", asked)
    with pytest.raises(eval_retrieval.ModelRefused):
        eval_retrieval.run_task(dict(TASK))
    assert asked.calls == []


def test_retrieval_eval_runs_an_ordinary_model(monkeypatch):
    import eval_retrieval

    conditions = []
    monkeypatch.setattr(
        eval_retrieval, "_run_condition",
        lambda prompt, check, ask: conditions.append(prompt) or (True, ""),
    )
    monkeypatch.setattr(server, "_make_generate", lambda *a, **k: Asked())
    eval_retrieval.run_task(dict(TASK), model=ALLOWED)
    assert len(conditions) == 2


# -- promotion eval ------------------------------------------------------------

def test_promotion_eval_refuses_a_denied_model_before_generating():
    import promotion_eval

    asked = Asked()
    with pytest.raises(ValueError, match=DENIED):
        promotion_eval.evaluate_model(DENIED, generate=asked)
    assert asked.calls == []


# -- arena-shooter example drivers --------------------------------------------

@pytest.mark.parametrize("call", ["generate", "repair", "repair_lines"])
def test_arena_driver_refuses_a_denied_tier(monkeypatch, tmp_path, call):
    driver = load(EXAMPLE / "build_with_sonder.py", "arena_shooter_driver_lane")
    monkeypatch.setattr(driver, "PROJECT", str(tmp_path))
    (tmp_path / "A.cs").write_text("using System;\n", encoding="utf-8")
    runtime, asked = fake_runtime(DENIED)
    args = {
        "generate": (runtime, "A.cs", "contract", "code", 10),
        "repair": (runtime, "A.cs", "contract", ["e"], "code", 10),
        "repair_lines": (runtime, "A.cs", [], "code"),
    }[call]
    with pytest.raises(SystemExit, match=DENIED):
        getattr(driver, call)(*args)
    assert asked.calls == []
    assert (tmp_path / "A.cs").read_text(encoding="utf-8") == "using System;\n"


def test_arena_driver_asks_a_permitted_tier(monkeypatch, tmp_path):
    driver = load(EXAMPLE / "build_with_sonder.py", "arena_shooter_driver_lane_ok")
    monkeypatch.setattr(driver, "PROJECT", str(tmp_path))
    runtime, asked = fake_runtime(ALLOWED)
    driver.generate(runtime, "A.cs", "contract", "code", 10)
    assert len(asked.calls) == 1


def test_arena_skeleton_refuses_a_denied_model_override(monkeypatch, tmp_path, capsys):
    sys.path.insert(0, str(EXAMPLE))
    try:
        driver = load(EXAMPLE / "build_skeleton.py", "arena_shooter_skeleton_lane")
    finally:
        sys.path.remove(str(EXAMPLE))
    runtime, asked = fake_runtime(ALLOWED)
    monkeypatch.setattr(driver, "server", runtime)
    monkeypatch.setattr(driver, "build_errors", lambda *a, **k: ([], True))
    monkeypatch.setattr(
        sys, "argv",
        ["build_skeleton.py", "--project", str(tmp_path), "--tiers", "code", "--model", DENIED],
    )
    assert driver.main() == 2
    assert DENIED in capsys.readouterr().out
    assert asked.calls == []
