"""Regression for the 48 serial, identically briefed greenfield workers."""
import ast
import hashlib
import os
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

import master_orchestrator as mo
from sonder_runtime.domain import fleet_briefing


@pytest.fixture
def server(monkeypatch):
    """Exercise the real entry function with inert host boundaries (no server startup)."""
    source = Path(__file__).resolve().parents[1] / "server.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    entry = next(node for node in tree.body if isinstance(node, ast.FunctionDef)
                 and node.name == "master_orchestrate")
    entry.decorator_list = []
    worker_calls = []

    def agent_worker(*args, **kwargs):
        worker_calls.append((args, kwargs))
        return lambda prompt: "proposal"

    scope = dict(
        master_orchestrator=mo, os=os, TIMEOUT=180,
        _maybe_live_reload=lambda: None,
        intents=SimpleNamespace(containment_egress_refusal=lambda task: ""),
        creative_router=SimpleNamespace(classify=lambda *a, **k: None),
        _runtime_lane_tier=lambda lane, tier="auto": "code",
        _is_cloud_tier=lambda tier: False,
        _orchestrator_worker=lambda *a, **k: lambda prompt: "proposal",
        _orchestrator_agent_worker=agent_worker,
        _master_timeout_policy=lambda *a: 150,
        _worker_calls=worker_calls,
    )
    monkeypatch.setitem(sys.modules, "sonder_runtime.bootstrap.strategy", SimpleNamespace(
        compose_fleet_strategy_observer=lambda *a, **k: None,
        try_compose_strategy_memory=lambda *a, **k: None,
        try_configured_strategy_rollout=lambda: None,
        try_configured_strategy_trace=lambda *a: None,
    ))
    exec(compile(ast.Module(body=[entry], type_ignores=[]), str(source), "exec"), scope)
    return SimpleNamespace(**scope)


@pytest.fixture
def queued(monkeypatch, server):
    calls = []
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(server.creative_router, "classify", lambda *a, **k: None)
    monkeypatch.setattr(mo, "max_agents", lambda: 48)
    monkeypatch.setattr(mo, "capacity", lambda *a, **k: {"worker_slots": 1})

    def start(task, **kwargs):
        calls.append((task, kwargs))
        return {"master_id": "master-test", "agents": list(range(kwargs["agents"])),
                "worker_slots": 1, "output": "RUNNING", "background": True,
                "output_workspace": (
                    "C:/state/master-test"
                    if kwargs.get("build_workspace") else ""
                )}

    monkeypatch.setattr(mo, "start_delegated", start)
    return calls


@pytest.mark.parametrize("slots,expected", [(1, 3), (4, 8), (32, 48)])
@pytest.mark.parametrize("mode", ["fleet", "swarm", "fanout"])
@pytest.mark.parametrize("agents", [0, None, -2, "0"])
def test_capacity_sizes_unspecified_fleet(server, queued, monkeypatch, slots, expected, mode, agents):
    monkeypatch.setattr(mo, "capacity", lambda *a, **k: {"worker_slots": slots})
    server.master_orchestrate("make me something cool", mode=mode, agents=agents)
    assert queued[0][1]["agents"] == expected


@pytest.mark.parametrize("agents,expected", [(1, 1), (7, 7), (99, 48)])
def test_explicit_count_keeps_existing_clamp(server, queued, agents, expected):
    server.master_orchestrate("make me something cool", mode="fleet", agents=agents)
    assert queued[0][1]["agents"] == expected


@pytest.mark.parametrize("task,expected,count", [
    ("fleet 0 make me something cool", "make me something cool", 3),
    ("fleet 7 make me something cool", "make me something cool", 7),
    ("swarm make me something cool", "make me something cool", 3),
    ("/master fanout 4 make me something cool", "make me something cool", 4),
    ("master fleet -2 make me something cool", "make me something cool", 3),
    ("  /MASTER FLEET +2 make me something cool", "make me something cool", 2),
    ("fleet 0 design a fleet 9 dashboard", "design a fleet 9 dashboard", 3),
    ("fleet 0 fleet management ideas", "fleet management ideas", 3),
    ("fleet 2.5 ideas", "2.5 ideas", 3),
    ("fleet 3D art", "3D art", 3),
    ("design a fleet 9 dashboard", "design a fleet 9 dashboard", 3),
    ("fleetwood 7 ideas", "fleetwood 7 ideas", 3),
    ("masterpiece fleet 7 ideas", "masterpiece fleet 7 ideas", 3),
    ("explain /master fleet 7 ideas", "explain /master fleet 7 ideas", 3),
    ('"fleet 7 ideas"', '"fleet 7 ideas"', 3),
])
def test_leading_prefix_only(server, queued, task, expected, count):
    server.master_orchestrate(task, mode="fleet")
    assert queued[0][0] == expected
    assert queued[0][1]["agents"] == count


def test_prefix_selects_fleet_and_api_count_wins(server, queued):
    server.master_orchestrate("/master fleet 9 make me something cool", agents=2)
    assert queued[0][0] == "make me something cool"
    assert queued[0][1]["agents"] == 2
    assert queued[0][1]["metadata"]["mode"] == "fleet"


def test_empty_routed_task_queues_nothing(server, queued):
    assert "empty task" in server.master_orchestrate("fleet 0")
    assert not queued


@pytest.mark.parametrize("task,cap,agents,expected", [
    ("use 12 workers to compare ideas", 0, 0, 12),
    ("fleet 0 use 12 workers to compare ideas", 0, 0, 12),
    ("fleet 0 compare ideas", 9, 0, 9),
    ("fleet 0 compare ideas", 9, 5, 5),
])
def test_worker_overrides_keep_existing_behavior(server, queued, monkeypatch, task, cap, agents, expected):
    monkeypatch.setattr(mo, "capacity", lambda n=None, worker_cap=None: {
        "requested_worker_cap": worker_cap, "worker_slots": worker_cap or 1,
    })
    server.master_orchestrate(task, mode="fleet", worker_cap=cap, agents=agents)
    assert queued[0][1]["agents"] == expected
    assert queued[0][1]["worker_cap"] == (cap or 12)


def test_advice_plan_shows_clean_task_breadth_slots_estimate_and_proposal_policy(server, queued):
    text = server.master_orchestrate("fleet 0 compare ideas")
    assert "queued 3 agent(s) across 1 worker slot(s)" in text
    assert "90s" in text and "30s per agent" in text and "default" in text
    assert "Task: compare ideas" in text and "fleet 0" not in text
    assert "proposals" in text and "no filesystem or shell tools" in text
    assert "/autopilot" in text and "project" in text
    assert "master_status()" in text and "master_cancel('master-test')" in text


def test_build_plan_names_workspace_and_equips_agent_worker(server, queued):
    text = server.master_orchestrate("fleet 0 make me something cool")
    assert "queued 3 agent(s) across 1 worker slot(s)" in text
    assert "Task: make me something cool" in text
    assert "C:/state/master-test" in text
    assert "Build workers create separate candidates" in text
    assert "proposals with no filesystem" not in text
    assert queued[0][1]["build_workspace"] is True
    assert server._worker_calls[0][1]["build"] is True


def test_angles_are_distinct_deterministic_and_subordinate(monkeypatch):
    monkeypatch.setattr(mo, "max_agents", lambda: 64)
    task = "make me something cool\nPreserve this exact line."
    prompts = mo._subtask_prompts(task, 64)
    assert prompts == mo._subtask_prompts(task, 64)
    angles = []
    for index, prompt in enumerate(prompts, 1):
        block = f"=== AUTHORITATIVE MASTER TASK ===\n{task}\n=== END AUTHORITATIVE MASTER TASK ==="
        assert prompt.startswith(block)
        angle, = [line for line in prompt.splitlines() if line.startswith("Angle ")]
        assert angle.startswith(f"Angle {index}/64:")
        angles.append(angle.split(":", 1)[1])
        assert "no filesystem, shell, web" in prompt
    assert len(set(angles)) == 64


def test_build_angles_are_distinct_and_implementation_focused():
    projects = [f"C:/state/worker-{index:02d}" for index in range(1, 5)]
    prompts = fleet_briefing.subtask_prompts(
        "make me something cool", len(projects), build_projects=projects
    )
    assert all(prompt.startswith("=== AUTHORITATIVE MASTER TASK ===") for prompt in prompts)
    angle_lines = [
        next(line for line in prompt.splitlines() if line.startswith("Angle "))
        for prompt in prompts
    ]
    assert len(set(angle_lines)) == len(projects)
    assert all("Implementation angle:" in line for line in angle_lines)
    assert all("proposal" not in line.casefold() for line in angle_lines)
    assert all("Build a working candidate" in prompt for prompt in prompts)


@pytest.mark.parametrize("tools,expected", [
    (False, "68cb2809b2b0933ac327797dea96eaafb57d656829b28d92c761e72a28a9ac9c"),
    (True, "de049fbee497a28a27f36f6042061a499fbf9783eb1f7beebe961a06567e31bb"),
])
def test_protected_objective_briefs_match_prechange_bytes(tools, expected):
    task = "Inspect exactly.\n[objective:a|file:a.py|symbol:a]"
    objectives = mo.fleet_provenance.parse_objectives(task)
    prompts = mo._subtask_prompts(task, 2, tool_access=tools, project="/repo",
                                  objective_assignments=(objectives, objectives))
    assert hashlib.sha256("\n".join(prompts).encode()).hexdigest() == expected
    assert all("Angle " not in prompt for prompt in prompts)


def test_objective_markers_without_assignments_and_single_worker_skip_angles():
    assert "Angle " not in mo._subtask_prompts("plain task", 1)[0]
    prompts = mo._subtask_prompts("Inspect.\n[objective:a|file:a.py|symbol:a]", 2)
    assert all("Angle " not in prompt for prompt in prompts)


def test_explicit_breadth_does_not_probe_capacity(monkeypatch):
    monkeypatch.setattr(mo, "max_agents", lambda: 12)
    monkeypatch.setattr(mo, "capacity", lambda: pytest.fail("explicit count probed capacity"))
    assert mo.fleet_agent_count(7) == 7


@pytest.mark.parametrize("ceiling", [1, 2])
def test_small_configured_ceiling_wins_over_minimum(monkeypatch, ceiling):
    monkeypatch.setattr(mo, "max_agents", lambda: ceiling)
    monkeypatch.setattr(mo, "capacity", lambda: {"worker_slots": 1})
    assert mo.fleet_agent_count() == ceiling


def test_ask_preview_matches_capacity_default(server, queued):
    text = server.master_orchestrate("compare ideas", mode="ask")
    assert "fleet of 3 agents on 1 worker slots" in text
    assert text.receipt_fields["orchestration"]["fleet_agents"] == 3
    assert text.receipt_fields["orchestration"]["worker_slots"] == 1
    assert not queued


def test_plain_delegate_keeps_three_agent_default(server, monkeypatch, queued):
    monkeypatch.setattr(mo, "run_delegated", lambda task, **k: {
        "master_id": "master-sync", "agents": list(range(mo.clamp_agent_count(k["agents"]))),
        "worker_slots": 1, "outputs": [], "output": "merged",
    })
    text = server.master_orchestrate("compare ideas", mode="delegate")
    assert "agents=3" in text and "merged" in text
    assert not queued


def test_repository_plan_and_parallel_plan_do_not_misstate_policy_or_time():
    parallel = fleet_briefing.format_plan("inspect\nfiles", 3, 3, greenfield=False)
    assert parallel == "queued 3 agent(s) across 3 worker slot(s). Task: inspect files"
    serial = fleet_briefing.format_plan("inspect files", 8, 3, greenfield=False)
    assert "~80s" in serial and "8/3 x 30s per agent" in serial
    assert "proposals" not in serial


def test_retry_does_not_reinterpret_persisted_task(server, queued):
    server.master_orchestrate("fleet management ideas", mode="fleet", agents=3,
                              retry_of="master-original")
    assert queued[0][0] == "fleet management ideas"
    assert queued[0][1]["metadata"]["retry_of"] == "master-original"


@pytest.mark.parametrize("surface", ["http/serve.py", "repl/repl.py"])
@pytest.mark.parametrize("mode", ["fleet", "swarm", "fanout"])
@pytest.mark.parametrize("count,expected", [(0, 3), (5, 5)])
def test_slash_handler_preserves_prefix_until_master(server, queued, surface, mode, count, expected):
    """Compile the actual slash branch without starting HTTP or an interactive REPL."""
    source = Path(__file__).resolve().parents[1] / "sonder_runtime/interfaces" / surface
    tree = ast.parse(source.read_text(encoding="utf-8"))
    branch, = [node for node in ast.walk(tree) if isinstance(node, ast.If)
               and ast.unparse(node.test) in (
                   "cmd in ('/master', '/master_orchestrate')",
                   "cmd in ('/master', '/master_orchestrate', '/delegate')",
               )]
    route = ast.parse("def route(arg): pass").body[0]
    route.body = branch.body
    from sonder_runtime.interfaces.orchestration_commands import execute_master_command
    host = SimpleNamespace(
        master_orchestrate=server.master_orchestrate, master_orchestrator=mo,
        control_command=lambda command, **kw: execute_master_command(
            command.split(None, 1)[1], orchestrate=server.master_orchestrate,
            capacity=mo.capacity, project=kw.get("project", ""),
        ),
    )
    scope = dict(server=host, context=None, _emit=lambda text: text,
                 _account_task_boundary_refusal=lambda *a: "", project="",
                 workspace_project=None, idempotency_key="", session_id="test",
                 line=f"/master {mode} {count} make me something cool", stripped="/master",
                 _idempotent_http_action=lambda _c, _k, _a, function: function(),
                 _narrate_http_command=lambda _n, _a, function, _c: function())
    exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])),
                 str(source), "exec"), scope)
    scope["route"](f"{mode} {count} make me something cool")
    assert queued[0][0] == "make me something cool"
    assert queued[0][1]["agents"] == expected
