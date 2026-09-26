"""The extracted /goal use case and its dispatcher wiring."""

import pytest

from sonder_runtime.application.goals import GoalCommandFailed, GoalCommandPorts, run_goal_command


class Store:
    class GoalError(Exception):
        pass

    def __init__(self):
        self.goal = {"id": "g-1", "objective": "ship", "criteria": ["a"]}

    def set_goal(self, objective, criteria, origin):
        return dict(self.goal, objective=objective)

    def adopt(self, goal_id, actor):
        if goal_id != "g-1":
            raise self.GoalError("no proposal %s" % goal_id)
        return self.goal


def ports(store, calls):
    return GoalCommandPorts(
        goal_store=store,
        format_goal=lambda goal: "goal: %s" % (goal or {}).get("objective"),
        goal_to_plan=lambda goal: {"step_count": 2},
        goal_to_autopilot=lambda goal, **kw: calls.append(kw) or {"run_id": "r-1"},
        launch_autopilot=lambda run_id: calls.append(("launch", run_id)),
        refresh_proposals=lambda: {"proposed": 0, "skipped": 0},
        resolve_project=lambda project: project or "default",
    )


def test_auto_goal_launches_autopilot_in_the_callers_project():
    calls = []
    out = run_goal_command(ports(Store(), calls), "set --auto ship it", project="engine", request_owner="me")

    assert calls[0] == {"project": "engine", "request_owner": "me"}
    assert calls[1] == ("launch", "r-1")
    assert "autopilot: r-1 started" in out


def test_auto_goal_without_project_uses_the_default_project():
    calls = []
    run_goal_command(ports(Store(), calls), "set --auto ship it")
    assert calls[0]["project"] == "default"


def test_adopt_formats_and_errors_are_reported():
    p = ports(Store(), [])
    assert run_goal_command(p, "adopt g-1") == "adopted\ngoal: ship"
    with pytest.raises(GoalCommandFailed, match="no proposal nope"):
        run_goal_command(p, "adopt nope")


def test_server_wrapper_renders_the_legacy_error_signal(monkeypatch):
    import goal_store
    import server

    def fail(goal_id, actor):
        raise goal_store.GoalError("no proposal %s" % goal_id)

    monkeypatch.setattr(goal_store, "adopt", fail)
    assert server._goal_command("adopt nope") == "ERROR: no proposal nope"


def test_dispatcher_threads_project_into_goal_command(monkeypatch):
    import server

    seen = {}
    monkeypatch.setattr(server, "_goal_command", lambda arg, **kw: seen.update(kw) or "ok")
    assert server.control_command("/goal show", project="engine") == "ok"
    assert seen["project"] == "engine"
