"""Goal-bound autopilot runs must be created with a concrete local tier.

``/goal set --auto``, ``/autopilot run --goal`` and ``/mission start --auto``
all launch through ``composition.goal_to_autopilot``, whose default tier is the
placeholder ``"auto"``.  ``autopilot_start`` resolves that placeholder through
the runtime policy's ``autopilot`` lane before creating a run, but the goal
bridge stored it verbatim.  The controller's first model call then rejected the
run (``autopilot accepts local tiers only``), so every goal-bound run failed
with "autopilot controller failed safely" after zero cycles.
"""
import json

import pytest

import autopilot_controller
import composition
import goal_store
from sonder_runtime.adapters import runtime_policy
from sonder_runtime.adapters.persistence import autopilot_store, composition_store


@pytest.fixture(autouse=True)
def _isolated_stores(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_GOAL_DB", str(tmp_path / "goals.db"))
    monkeypatch.setenv("SONDER_AUTOPILOT_DB", str(tmp_path / "autopilot.db"))
    monkeypatch.setenv("SONDER_COMPOSITION_DB", str(tmp_path / "composition.db"))
    monkeypatch.setenv("SONDER_RUNTIME_POLICY", str(tmp_path / "runtime_policy.json"))
    goal_store.reset_for_tests()
    autopilot_store.reset_schema_cache_for_tests()
    yield
    connection = getattr(composition_store._LOCAL, "comp_conn", None)
    if connection is not None:
        connection.close()
        composition_store._LOCAL.comp_conn = None
        composition_store._LOCAL.comp_path = None
    goal_store.reset_for_tests()
    autopilot_store.reset_schema_cache_for_tests()


def _route_autopilot_lane(tmp_path, tier):
    policy = runtime_policy.load(create=True)
    routing = dict(policy.get("routing") or {})
    routing["autopilot"] = tier
    data = json.loads((tmp_path / "runtime_policy.json").read_text(encoding="utf-8"))
    data["routing"] = routing
    (tmp_path / "runtime_policy.json").write_text(json.dumps(data), encoding="utf-8")
    assert runtime_policy.route_tier("autopilot") == tier


@pytest.mark.parametrize("placeholder", ["auto", "", "default", "policy", "AUTO"])
def test_goal_bound_run_stores_the_autopilot_lane_tier(placeholder):
    goal = goal_store.set_goal("write hello.txt", "hello.txt contains hi")

    result = composition.goal_to_autopilot(goal, tier=placeholder)

    assert "error" not in result, result
    run = autopilot_store.get_run(result["run_id"])
    # The controller re-normalizes the stored tier on every model call; a
    # placeholder here is what made the run fail before its first cycle.
    assert autopilot_controller.normalize_tier(run["tier"]) == run["tier"]
    assert run["tier"] == runtime_policy.route_tier("autopilot")


def test_goal_bound_run_follows_the_configured_autopilot_lane(tmp_path):
    _route_autopilot_lane(tmp_path, "general")
    goal = goal_store.set_goal("write hello.txt")

    result = composition.goal_to_autopilot(goal)

    assert autopilot_store.get_run(result["run_id"])["tier"] == "general"


def test_explicit_local_tier_is_kept():
    goal = goal_store.set_goal("write hello.txt")

    result = composition.goal_to_autopilot(goal, tier="Fast")

    assert autopilot_store.get_run(result["run_id"])["tier"] == "fast"


def test_invalid_tier_is_refused_before_a_doomed_run_is_created():
    goal = goal_store.set_goal("write hello.txt")

    result = composition.goal_to_autopilot(goal, tier="cloud-code")

    assert "local tiers only" in result.get("error", "")
    assert "run_id" not in result
    assert autopilot_store.list_runs() == []
    assert composition_store.lookup_targets("goal", goal["id"], "autopilot") == []


def test_mission_start_auto_creates_a_runnable_run():
    result = composition.mission_start("ship it", "tests pass", auto=True)

    run_id = result["autopilot"]["run_id"]
    tier = autopilot_store.get_run(run_id)["tier"]
    assert autopilot_controller.normalize_tier(tier) == tier


def test_mission_start_tool_without_auto_reports_the_goal(monkeypatch):
    """The MCP tool read ``result.get("autopilot", {})``, but the bridge
    returns ``autopilot: None`` when ``auto`` is off (the default), so the
    tool raised AttributeError after the goal had already been set."""
    import server

    monkeypatch.setattr(
        server, "_launch_autopilot",
        lambda *_a, **_k: pytest.fail("auto=False must not launch a run"),
    )

    out = server.mission_start("ship it", "tests pass")

    assert out.startswith("mission started")
    assert "ship it" in out
    assert "autopilot:" not in out
    assert goal_store.get_active()["objective"] == "ship it"
