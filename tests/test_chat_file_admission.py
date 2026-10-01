"""Exercise production routing seams without importing the live legacy host.

Only function definitions are loaded from source; their dependency boundaries
are injected. Full model/tool integration lives in test_chat_tool_routing_integration.
"""
import ast
import contextlib
import sys
import time
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

import intents
from sonder_runtime.application.chat.lanes import ChatHandoffProvenance, ChatLaneService
from sonder_runtime.domain.execution_route_formatting import execution_route_header

ROOT = Path(__file__).resolve().parents[1]


def _function(path, name, namespace):
    tree = ast.parse((ROOT / path).read_text(encoding="utf-8"))
    node = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)
    node.decorator_list = []
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


@pytest.mark.parametrize(("prompt", "lane"), [
    ("how many python files are in the sonder_runtime folder, and which one is the biggest?", "inspection"),
    ("Read README.md", "inspection"),
    ("Write a Python script and save it as primes.py", "workbench"),
    ("/delegate write a Python script and save it as primes.py", "workbench"),
    ("How do I read foo.py?", "chat"),
    ("Show me how to write app.py", "chat"),
    ("Hello!", "chat"),
    ("Read foo.py without tools", "chat"),
])
def test_real_classifier_and_typed_handoff(prompt, lane):
    decision = ChatLaneService(intents.classify_execution).decide(
        prompt, project="default",
        provenance=ChatHandoffProvenance(surface="test", reason="file route regression"),
    )
    assert decision.lane == lane
    if lane == "chat":
        assert decision.handoff is None
    else:
        assert decision.handoff.objective == prompt
        assert decision.handoff.project == "default"


@pytest.mark.parametrize("pinned_tier", ["", "qwen-pinned"])
def test_production_router_reaches_read_only_agent_and_renders_route(monkeypatch, pinned_tier):
    module = ModuleType("chat_route_fixture")
    calls = []
    module.unsafe_lab = SimpleNamespace(active=lambda: False)
    module.served_work_project = lambda project: project
    module._agent_impl = lambda prompt, **kw: calls.append((prompt, kw)) or "3 files from tools"
    monkeypatch.setitem(sys.modules, module.__name__, module)
    scope = {
        "__name__": module.__name__, "sys": sys, "contextlib": contextlib, "intents": intents,
        "_maybe_live_reload": lambda: None,
        "master_orchestrator": SimpleNamespace(requested_worker_cap=lambda _p: None),
        "runtime_policy": SimpleNamespace(ROUTING_LANES={"workbench"}, route_tier=lambda *_a, **_kw: "code"),
        "_RUNTIME_POLICY": {}, "_resolve_project": lambda project: project,
        "_capability_refined_tier": lambda _p, tier, reason: (tier, reason),
        "_execution_route_header": execution_route_header,
    }
    route = _function("server.py", "_route_work_request", scope)
    assert route("Hello", project="project") is None
    output = route("How many files are in this folder?", project="project", _tool_tier=pinned_tier)
    assert "read-only workspace inspection" in output
    assert "3 files from tools" in output
    assert len(calls) == 1
    assert calls[0][1]["read_only"] is True
    assert calls[0][1]["project"] == "project"
    assert calls[0][1]["tier"] == (pinned_tier or "code")


def test_production_compound_router_preserves_bounded_legacy_dispatch():
    prompt = (
        "Inspect the repository, diagnose the API, and then fix the app before "
        "you run and validate all tests."
    )
    choices, calls = [], []

    def choose(value, **kwargs):
        choices.append((value, kwargs))
        return {"mode": "workbench", "tier": "general", "reason": "one guarded loop", "confidence": 0.8}

    def workbench(value, tier, **kwargs):
        calls.append((value, tier, kwargs))
        return "work complete", tier

    scope = {
        "__name__": __name__, "sys": sys, "contextlib": contextlib, "intents": intents,
        "_maybe_live_reload": lambda: None,
        "master_orchestrator": SimpleNamespace(requested_worker_cap=lambda _p: None),
        "runtime_policy": SimpleNamespace(ROUTING_LANES={"workbench"}, route_tier=lambda *_a, **_kw: "code"),
        "_RUNTIME_POLICY": {}, "_resolve_project": lambda project: project,
        "_capability_refined_tier": lambda _p, tier, reason: (tier, reason),
        "_execution_route_header": execution_route_header,
        "npu_service": SimpleNamespace(routing_active=lambda: "off", route_decide=lambda _p: None),
        "_execution_route_model": choose, "_workbench_agent_escalating": workbench,
    }
    route = _function("server.py", "_route_work_request", scope)
    output = route(prompt, project="demo")
    assert choices == [(prompt, {"project": "demo"})]
    assert calls == [(prompt, "general", {
        "max_steps": 12, "allow_web": True, "project": "demo", "allow_location": False,
    })]
    assert "source: bounded local mode model" in output
    assert "confidence: 80%" in output
    assert output.endswith("work complete")


@pytest.mark.parametrize("reply,needs_note", [
    ("Run it with `python primes.py`.", True),
    ("`primes.py` was saved.", True),
    ("Hello!", False),
])
def test_production_plain_http_postcheck(reply, needs_note):
    # Fake just the provider/transport collaborators. The production _run_prompt
    # function performs the final text check itself; no tools are claimed.
    state = SimpleNamespace(trace=False, strict=False)
    scope = {
        "_serve_logger": SimpleNamespace(debug=lambda *_a: None),
        "_state_or_legacy": lambda _s: state, "time": time,
        "server": SimpleNamespace(
            answer_with_history=lambda *_a, **_kw: reply,
            parse_interaction_id=lambda _s: None, served_work_project=lambda _s: "",
        ),
        "_answer_only": lambda text: text, "_strip_footer": lambda text: text,
        "_turn_reasoning": lambda: "", "TurnResult": lambda content, *_args: SimpleNamespace(content=content),
    }
    run = _function("sonder_runtime/interfaces/http/serve.py", "_run_prompt", scope)
    result = run("Hello", return_result=True)
    assert ("(Not saved" in result.content) == needs_note
    if not needs_note:
        assert result.content == reply


def test_production_http_project_mapping_preserves_non_file_turns():
    scope = {"server": SimpleNamespace(served_work_project=lambda _p: ""), "intents": intents}
    project = _function("sonder_runtime/interfaces/http/serve.py", "_work_project_for_request", scope)
    assert project("default", "private-project-id", "save primes.py") == "default"
    assert project("", "private-project-id", "save primes.py") == "default"
    assert project("default", "private-project-id", "Hello") == "private-project-id"


def test_unknown_file_mode_cannot_start_any_host_operation():
    from sonder_runtime.adapters.chat_file_routing import route_file_request
    result, tier = route_file_request(object(), "save x.py", "unrecognized", "default", "code")
    assert result.startswith("Refused:") and tier == "code"


def test_oversized_file_turn_keeps_legacy_chat_limit_behavior():
    assert intents.classify_execution("Read foo.py " + "x" * 12_000) is None
