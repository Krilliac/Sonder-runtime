"""Tests for cross-subsystem composition bridges.

Exercises the composition store, the composition service, and the server-level
integration points without requiring a live model or Ollama.
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

os.environ.setdefault("SONDER_ALLOW_CLOUD", "0")

import sonder_paths


class _TempPaths:
    """Redirect all Sonder state paths to a temporary directory."""

    def __init__(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig = None

    def __enter__(self):
        self._orig = sonder_paths.state_path
        d = self._tmpdir.name
        sonder_paths.state_path = lambda name, env_key=None: os.path.join(d, name)
        return d

    def __exit__(self, *exc):
        sonder_paths.state_path = self._orig
        self._tmpdir.cleanup()


class TestCompositionStore(unittest.TestCase):
    """Binding persistence layer."""

    def setUp(self):
        self._paths = _TempPaths()
        self._paths.__enter__()
        from sonder_runtime.adapters.persistence import composition_store
        self.store = composition_store
        import importlib
        importlib.reload(composition_store)

    def tearDown(self):
        self._paths.__exit__(None, None, None)

    def test_bind_creates_and_retrieves(self):
        b = self.store.bind("goal", "g-1", "autopilot", "auto-1", kind="drives")
        self.assertEqual(b["source_type"], "goal")
        self.assertEqual(b["source_id"], "g-1")
        self.assertEqual(b["target_type"], "autopilot")
        self.assertEqual(b["target_id"], "auto-1")
        self.assertEqual(b["kind"], "drives")
        self.assertEqual(b["status"], "active")

    def test_bind_idempotent(self):
        b1 = self.store.bind("goal", "g-1", "autopilot", "auto-1")
        b2 = self.store.bind("goal", "g-1", "autopilot", "auto-1")
        self.assertEqual(b1["id"], b2["id"])

    def test_lookup_targets(self):
        self.store.bind("goal", "g-1", "autopilot", "auto-1")
        self.store.bind("goal", "g-1", "task", "task-1", kind="decomposes")
        targets = self.store.lookup_targets("goal", "g-1")
        self.assertEqual(len(targets), 2)
        types = {t["target_type"] for t in targets}
        self.assertEqual(types, {"autopilot", "task"})

    def test_lookup_targets_filtered(self):
        self.store.bind("goal", "g-1", "autopilot", "auto-1")
        self.store.bind("goal", "g-1", "task", "task-1")
        targets = self.store.lookup_targets("goal", "g-1", target_type="autopilot")
        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0]["target_type"], "autopilot")

    def test_lookup_sources(self):
        self.store.bind("goal", "g-1", "autopilot", "auto-1")
        sources = self.store.lookup_sources("autopilot", "auto-1")
        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["source_type"], "goal")

    def test_complete_binding(self):
        b = self.store.bind("goal", "g-1", "autopilot", "auto-1")
        closed = self.store.complete_binding(b["id"], reason="done")
        self.assertEqual(closed["status"], "completed")
        active = self.store.lookup_targets("goal", "g-1")
        self.assertEqual(len(active), 0)

    def test_break_binding(self):
        b = self.store.bind("goal", "g-1", "autopilot", "auto-1")
        broken = self.store.break_binding(b["id"], reason="failed")
        self.assertEqual(broken["status"], "broken")

    def test_active_bindings(self):
        self.store.bind("goal", "g-1", "autopilot", "auto-1")
        self.store.bind("goal", "g-2", "task", "task-2")
        active = self.store.active_bindings()
        self.assertEqual(len(active), 2)

    def test_close_all_for(self):
        self.store.bind("goal", "g-1", "autopilot", "auto-1")
        self.store.bind("goal", "g-1", "task", "task-1")
        self.store.bind("goal", "g-2", "autopilot", "auto-2")
        closed = self.store.close_all_for("goal", "g-1", status="completed")
        self.assertEqual(closed, 2)
        remaining = self.store.active_bindings()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["source_id"], "g-2")

    def test_invalid_types_raise(self):
        with self.assertRaises(self.store.CompositionStoreError):
            self.store.bind("invalid", "x", "goal", "y")
        with self.assertRaises(self.store.CompositionStoreError):
            self.store.bind("goal", "x", "invalid", "y")

    def test_invalid_kind_raises(self):
        with self.assertRaises(self.store.CompositionStoreError):
            self.store.bind("goal", "x", "autopilot", "y", kind="invalid")

    def test_metadata_persisted(self):
        b = self.store.bind(
            "goal", "g-1", "autopilot", "auto-1",
            metadata={"objective": "test objective"},
        )
        self.assertEqual(b["metadata"]["objective"], "test objective")


class TestCompositionService(unittest.TestCase):
    """Bridge logic connecting subsystems."""

    def setUp(self):
        self._paths = _TempPaths()
        self._paths.__enter__()
        from sonder_runtime.adapters.persistence import composition_store
        import importlib
        importlib.reload(composition_store)

    def tearDown(self):
        self._paths.__exit__(None, None, None)

    def test_mission_start_creates_goal(self):
        import composition
        result = composition.mission_start("test objective", "criterion A; criterion B")
        goal = result["goal"]
        self.assertIsNotNone(goal)
        self.assertEqual(goal["objective"], "test objective")
        self.assertEqual(goal["status"], "active")
        self.assertIn("criterion A", goal.get("criteria", []))

    def test_mission_start_with_plan(self):
        import composition
        result = composition.mission_start(
            "build feature", "write tests; implement code; review",
            plan=True,
        )
        self.assertIsNotNone(result["plan"])
        self.assertEqual(result["plan"]["step_count"], 3)

    def test_mission_start_without_criteria_no_plan(self):
        import composition
        result = composition.mission_start("simple goal", plan=True)
        plan = result.get("plan")
        self.assertTrue(plan is None or plan.get("error"))

    def test_mission_status_shows_goal(self):
        import composition
        composition.mission_start("my objective", "a; b")
        status = composition.mission_status()
        self.assertIsNotNone(status["goal"])
        self.assertEqual(status["goal"]["objective"], "my objective")

    def test_goal_to_plan_decomposes_criteria(self):
        import goal_store
        import composition
        goal = goal_store.set_goal("fix all bugs", "lint; test; deploy")
        plan = composition.goal_to_plan(goal)
        self.assertEqual(plan["step_count"], 3)
        self.assertEqual(plan["goal_id"], goal["id"])
        titles = [s["title"] for s in plan["steps"]]
        self.assertIn("lint", titles)

    def test_goal_to_autopilot_creates_binding(self):
        import goal_store
        import composition
        from sonder_runtime.adapters.persistence import composition_store

        goal = goal_store.set_goal("automate this", "criterion 1; criterion 2")
        with mock.patch(
            "sonder_runtime.adapters.persistence.autopilot_store.create_run",
            return_value={"id": "auto-test123", "status": "ready"},
        ):
            result = composition.goal_to_autopilot(goal)

        self.assertEqual(result["run_id"], "auto-test123")
        self.assertEqual(result["goal_id"], goal["id"])
        bindings = composition_store.lookup_targets("goal", goal["id"], "autopilot")
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["target_id"], "auto-test123")

    def test_on_autopilot_terminal_updates_goal(self):
        import goal_store
        import composition
        from sonder_runtime.adapters.persistence import composition_store

        goal = goal_store.set_goal("test autopilot bridge")
        composition_store.bind("goal", goal["id"], "autopilot", "auto-done")

        effects = composition.on_autopilot_terminal({
            "id": "auto-done",
            "status": "completed",
            "summary": "all tasks passed",
        })
        self.assertTrue(effects["goal_updated"])
        self.assertEqual(effects["bindings_closed"], 1)
        updated = goal_store.get_active()
        notes = updated.get("notes", [])
        self.assertTrue(any("autopilot completed" in n.get("text", "") for n in notes))

    def test_on_autopilot_terminal_failure_notes(self):
        import goal_store
        import composition
        from sonder_runtime.adapters.persistence import composition_store

        goal = goal_store.set_goal("failing objective")
        composition_store.bind("goal", goal["id"], "autopilot", "auto-fail")

        effects = composition.on_autopilot_terminal({
            "id": "auto-fail",
            "status": "failed",
            "summary": "validation failed",
        })
        self.assertTrue(effects["goal_updated"])
        updated = goal_store.get_active()
        notes = updated.get("notes", [])
        self.assertTrue(any("autopilot failed" in n.get("text", "") for n in notes))

    def test_plan_to_workflow(self):
        import composition
        steps = [
            {"title": "write tests"},
            {"title": "implement feature"},
            {"title": "run validation"},
        ]
        result = composition.plan_to_workflow(steps, "my-workflow", goal_id="g-test")
        self.assertEqual(result["workflow_name"], "my-workflow")
        self.assertEqual(result["action_count"], 3)
        self.assertEqual(result["actions"][0]["type"], "sonder")

    def test_on_workflow_complete_notes_goal(self):
        import goal_store
        import composition
        from sonder_runtime.adapters.persistence import composition_store

        goal = goal_store.set_goal("workflow objective")
        composition_store.bind("goal", goal["id"], "workflow", "test-workflow")

        effects = composition.on_workflow_complete(
            "test-workflow", {"ok": True},
        )
        self.assertTrue(effects["goal_noted"])
        updated = goal_store.get_active()
        notes = updated.get("notes", [])
        self.assertTrue(any("workflow" in n.get("text", "") for n in notes))

    def test_selfmod_observations_to_goals(self):
        import composition
        observations = [
            {"description": "refactor the loop module", "severity": "low"},
            {"description": "fix import cycle", "files": ["a.py", "b.py"]},
            {"description": ""},
        ]
        result = composition.selfmod_observations_to_goals(observations)
        self.assertEqual(result["proposed"], 2)
        self.assertEqual(result["skipped"], 1)

    def test_training_to_campaign(self):
        import composition
        task = {
            "id": "train-001",
            "prompt": "Write fizzbuzz",
            "language": "python",
            "assert_checks": ["def fizzbuzz"],
        }
        result = composition.training_to_campaign(task)
        self.assertEqual(result["task_id"], "train-001")
        self.assertEqual(result["language"], "python")
        self.assertEqual(len(result["phases"]), 4)
        self.assertIn("generate", result["phases"])

    def test_fleet_evidence_to_goal(self):
        import goal_store
        import composition
        goal = goal_store.set_goal(
            "ship feature",
            "all tests pass; docs updated; reviewed",
        )
        fleet_results = [
            {"output": "All tests pass - 42/42 green"},
            {"output": "docs updated with the new API reference"},
        ]
        result = composition.fleet_evidence_to_goal(fleet_results, goal["id"])
        self.assertEqual(result["criteria_matched"], 2)
        self.assertEqual(result["criteria_total"], 3)
        self.assertEqual(len(result["unmatched"]), 1)

    def test_preferences_to_emotion_adjustments(self):
        import composition
        lessons = [
            {"text": "user prefers concise responses"},
            {"text": "be more friendly and warm"},
            {"text": "always be precise with numbers"},
        ]
        result = composition.preferences_to_emotion_adjustments(lessons)
        adj = result["adjustments"]
        self.assertGreater(adj.get("brevity", 0), 0)
        self.assertGreater(adj.get("warmth", 0), 0)
        self.assertGreater(adj.get("precision", 0), 0)
        self.assertFalse(result["applied"])

    def test_autopilot_outcomes_to_memory(self):
        import composition
        from sonder_runtime.adapters.persistence import composition_store

        run = {
            "id": "auto-mem-test",
            "objective": "test memory bridge",
            "plan": [
                {"id": "task-01", "title": "inspect", "kind": "inspect", "status": "passed"},
                {"id": "task-02", "title": "implement", "kind": "implement", "status": "failed"},
                {"id": "task-03", "title": "validate", "kind": "validate", "status": "pending"},
            ],
        }
        result = composition.autopilot_outcomes_to_memory(run)
        self.assertEqual(result["recorded"], 2)
        self.assertEqual(result["total_tasks"], 3)
        bindings = composition_store.lookup_targets("autopilot", "auto-mem-test", "memory")
        self.assertEqual(len(bindings), 2)

    def test_composition_status(self):
        import composition
        from sonder_runtime.adapters.persistence import composition_store
        composition_store.bind("goal", "g-1", "autopilot", "a-1")
        composition_store.bind("goal", "g-1", "task", "t-1", kind="decomposes")
        status = composition.composition_status()
        self.assertEqual(status["active_bindings"], 2)
        self.assertIn("drives", status["by_kind"])
        self.assertIn("decomposes", status["by_kind"])

    def test_format_mission_status_no_goal(self):
        import composition
        text = composition.format_mission_status({"goal": None})
        self.assertIn("no active mission", text)

    def test_format_mission_status_with_data(self):
        import composition
        data = {
            "goal": {
                "id": "g-test", "status": "active",
                "objective": "build the thing",
                "criteria": ["tests pass", "docs updated"],
                "notes": [{"text": "started work"}],
            },
            "autopilot_runs": [
                {"id": "auto-1", "status": "running", "phase": "execute", "plan": [
                    {"status": "passed"}, {"status": "pending"},
                ]},
            ],
            "task_plans": [{"binding_id": "b-1", "task_id": "t-1"}],
            "workflows": [],
            "bindings": [{"id": "b-1"}, {"id": "b-2"}],
        }
        text = composition.format_mission_status(data)
        self.assertIn("build the thing", text)
        self.assertIn("autopilot: auto-1", text)
        self.assertIn("1 passed", text)
        self.assertIn("task plans: 1 linked", text)
        self.assertIn("bindings: 2 active", text)


class TestServerIntegration(unittest.TestCase):
    """Server-level command integration."""

    def setUp(self):
        self._paths = _TempPaths()
        self._paths.__enter__()
        from sonder_runtime.adapters.persistence import composition_store
        import importlib
        importlib.reload(composition_store)

    def tearDown(self):
        self._paths.__exit__(None, None, None)

    def test_mission_command_status_empty(self):
        import server
        result = server.control_command("/mission status")
        self.assertIn("no active mission", result)

    def test_mission_command_start(self):
        import server
        result = server.control_command("/mission start build widgets --criteria lint; test")
        self.assertIn("mission started", result)
        self.assertIn("build widgets", result)

    def test_mission_command_start_with_plan(self):
        import server
        result = server.control_command(
            "/mission start --plan optimize queries --criteria profile; index; benchmark"
        )
        self.assertIn("mission started", result)
        self.assertIn("3 steps", result)

    def test_mission_command_done(self):
        import server
        import goal_store
        goal_store.set_goal("test done")
        result = server.control_command("/mission done completed successfully")
        self.assertIn("mission completed", result)

    def test_mission_command_abandon(self):
        import server
        import goal_store
        goal_store.set_goal("test abandon")
        result = server.control_command("/mission abandon not needed")
        self.assertIn("mission abandoned", result)

    def test_mission_command_bindings(self):
        import server
        result = server.control_command("/mission bindings")
        self.assertIn("composition status", result)

    def test_mission_command_help(self):
        import server
        result = server.control_command("/mission help")
        self.assertIn("usage:", result)

    def test_goal_set_with_auto_flag(self):
        import server
        with mock.patch(
            "sonder_runtime.adapters.persistence.autopilot_store.create_run",
            return_value={"id": "auto-flag-test", "status": "ready"},
        ), mock.patch(
            "server._launch_autopilot",
            return_value=True,
        ):
            result = server.control_command(
                "/goal set --auto run the tests --criteria all green",
                operator_approved=True,
            )
        self.assertIn("goal set", result)
        self.assertIn("autopilot", result.lower())

    def test_goal_set_with_plan_flag(self):
        import server
        result = server.control_command(
            "/goal set --plan fix bugs --criteria lint; test; deploy",
            operator_approved=True,
        )
        self.assertIn("goal set", result)
        self.assertIn("3 steps", result)

    def test_autopilot_start_with_goal_flag(self):
        import server
        import goal_store
        goal_store.set_goal("goal for autopilot", "pass tests; deploy")
        with mock.patch(
            "sonder_runtime.adapters.persistence.autopilot_store.create_run",
            return_value={"id": "auto-goal-bind", "status": "ready"},
        ), mock.patch(
            "server._launch_autopilot",
            return_value=True,
        ):
            result = server.control_command("/autopilot start --goal")
        self.assertIn("bound to goal", result)
        self.assertIn("auto-goal-bind", result)

    def test_autopilot_start_goal_flag_no_goal(self):
        import server
        result = server.control_command("/autopilot start --goal")
        self.assertIn("ERROR", result)
        self.assertIn("active goal", result)


class TestEndToEnd(unittest.TestCase):
    """Full lifecycle: mission start -> status -> complete."""

    def setUp(self):
        self._paths = _TempPaths()
        self._paths.__enter__()
        from sonder_runtime.adapters.persistence import composition_store
        import importlib
        importlib.reload(composition_store)

    def tearDown(self):
        self._paths.__exit__(None, None, None)

    def test_full_mission_lifecycle(self):
        import goal_store
        import composition
        from sonder_runtime.adapters.persistence import composition_store

        result = composition.mission_start(
            "deliver release 2.0",
            "all tests green; docs updated; changelog written",
            plan=True,
        )
        goal = result["goal"]
        self.assertEqual(goal["status"], "active")
        self.assertEqual(result["plan"]["step_count"], 3)

        status = composition.mission_status()
        self.assertIsNotNone(status["goal"])
        self.assertEqual(status["goal"]["id"], goal["id"])
        self.assertTrue(len(status["bindings"]) >= 1)

        goal_store.add_note("tests are passing now")
        goal_store.add_note("docs written")

        closed = goal_store.complete("release shipped", actor="user")
        self.assertEqual(closed["status"], "completed")
        composition_store.close_all_for("goal", goal["id"])

        post_status = composition.mission_status()
        self.assertIsNone(post_status["goal"])

    def test_goal_autopilot_workflow_chain(self):
        """Goal -> Autopilot -> Terminal -> Goal note + Memory."""
        import goal_store
        import composition
        from sonder_runtime.adapters.persistence import composition_store

        goal = goal_store.set_goal("chain test", "step A; step B")

        with mock.patch(
            "sonder_runtime.adapters.persistence.autopilot_store.create_run",
            return_value={"id": "auto-chain", "status": "ready"},
        ):
            ap = composition.goal_to_autopilot(goal)
        self.assertEqual(ap["run_id"], "auto-chain")

        plan_result = composition.goal_to_plan(goal)
        self.assertEqual(plan_result["step_count"], 2)

        wf = composition.plan_to_workflow(
            plan_result["steps"], "chain-workflow", goal_id=goal["id"],
        )
        self.assertEqual(wf["action_count"], 2)

        effects = composition.on_autopilot_terminal({
            "id": "auto-chain",
            "status": "completed",
            "summary": "all good",
            "plan": [
                {"id": "t-01", "title": "A", "kind": "implement", "status": "passed"},
                {"id": "t-02", "title": "B", "kind": "validate", "status": "passed"},
            ],
        })
        self.assertTrue(effects["goal_updated"])

        mem = composition.autopilot_outcomes_to_memory({
            "id": "auto-chain",
            "objective": "chain test",
            "plan": [
                {"id": "t-01", "title": "A", "kind": "implement", "status": "passed"},
                {"id": "t-02", "title": "B", "kind": "validate", "status": "passed"},
            ],
        })
        self.assertEqual(mem["recorded"], 2)

        status = composition.composition_status()
        self.assertTrue(status["active_bindings"] >= 1)

    def test_selfmod_training_persona_bridges(self):
        """SelfMod -> Goals, Training -> Campaign, Preferences -> Emotion."""
        import composition

        sm = composition.selfmod_observations_to_goals([
            {"description": "dead code in loop.py", "severity": "low"},
            {"description": "unused import in server.py", "files": ["server.py"]},
        ])
        self.assertEqual(sm["proposed"], 2)

        tc = composition.training_to_campaign({
            "id": "t-fizz",
            "prompt": "FizzBuzz",
            "language": "python",
            "assert_checks": ["def fizzbuzz"],
        })
        self.assertEqual(tc["language"], "python")
        self.assertEqual(len(tc["phases"]), 4)

        pe = composition.preferences_to_emotion_adjustments([
            {"text": "be more concise and direct"},
            {"text": "I prefer creative solutions"},
        ])
        self.assertGreater(pe["adjustments"].get("brevity", 0), 0)
        self.assertGreater(pe["adjustments"].get("creativity", 0), 0)


if __name__ == "__main__":
    unittest.main()
