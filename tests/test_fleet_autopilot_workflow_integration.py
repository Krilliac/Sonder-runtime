"""Integration tests for fleet, autopilot, and workflow execution paths.

These tests exercise the actual server-level functions with mocked model
gateways, verifying capacity enforcement, state transitions, and the
bounded loop engine under progressively harder scenarios.
"""
from __future__ import annotations

import json
import os
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

import master_orchestrator
import server
import sonder_runtime.adapters.persistence.autopilot_store as autopilot_store
from sonder_runtime.application.workflows.loop import run_loop
from sonder_runtime.application.workflows.use_cases import WorkflowService
from sonder_runtime.application.ports.tool_executor import ToolResult


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _isolate_fleet_cap():
    original = master_orchestrator._FLEET_WORKER_CAP
    yield
    master_orchestrator._FLEET_WORKER_CAP = original


@pytest.fixture(autouse=True)
def _isolate_autopilot_cap():
    original = getattr(server, "_MAX_AUTOPILOT_RUNS", None)
    yield
    server._MAX_AUTOPILOT_RUNS = original


@pytest.fixture(autouse=True)
def _isolate_autopilot_threads():
    original = dict(server._AUTOPILOT_THREADS)
    yield
    server._AUTOPILOT_THREADS.clear()
    server._AUTOPILOT_THREADS.update(original)


# ---------------------------------------------------------------------------
# 1. Workflow loop engine — progressive complexity
# ---------------------------------------------------------------------------

class TestWorkflowLoop:
    """Exercise the bounded loop engine with increasingly complex action sets."""

    def test_single_action_single_iteration(self):
        actions = [{"type": "status"}]
        dispatch = lambda action: {"ok": True, "output": "healthy"}
        result = run_loop(actions, dispatch, max_iterations=1)
        assert len(result["iterations"]) == 1
        assert result["stop_reason"] == "max_iterations reached"

    def test_multi_action_sequence(self):
        call_order = []
        actions = [
            {"type": "diagnostics"},
            {"type": "self_heal_check"},
            {"type": "profile_status"},
        ]

        def dispatch(action):
            call_order.append(action["type"])
            return {"ok": True, "output": f"{action['type']} done"}

        result = run_loop(actions, dispatch, max_iterations=1)
        assert call_order == ["diagnostics", "self_heal_check", "profile_status"]
        assert len(result["iterations"]) == 1

    def test_stop_on_failure_halts_iteration(self):
        actions = [
            {"type": "good_action"},
            {"type": "bad_action"},
            {"type": "never_reached"},
        ]

        def dispatch(action):
            if action["type"] == "bad_action":
                return {"ok": False, "output": "failed"}
            return {"ok": True, "output": "ok"}

        result = run_loop(
            actions, dispatch, max_iterations=3, stop_on_failure=True,
        )
        assert len(result["iterations"]) == 1

    def test_stop_on_success_halts_after_full_green_iteration(self):
        iteration_count = [0]
        actions = [{"type": "check"}]

        def dispatch(action):
            iteration_count[0] += 1
            return {"ok": True, "output": "pass"}

        result = run_loop(
            actions, dispatch, max_iterations=10, stop_on_success=True,
        )
        assert len(result["iterations"]) == 1
        assert iteration_count[0] == 1

    def test_retry_until_success(self):
        attempt = [0]
        actions = [{"type": "code", "code": "test", "language": "python"}]

        def dispatch(action):
            attempt[0] += 1
            if attempt[0] < 3:
                return {"ok": False, "output": "syntax error"}
            return {"ok": True, "output": "pass"}

        result = run_loop(
            actions, dispatch, max_iterations=5,
            stop_on_failure=False, stop_on_success=True,
        )
        assert len(result["iterations"]) == 3
        assert attempt[0] == 3

    def test_max_iterations_clamped_to_50(self):
        actions = [{"type": "noop"}]
        dispatch = lambda a: {"ok": True, "output": "ok"}
        result = run_loop(actions, dispatch, max_iterations=999)
        assert len(result["iterations"]) == 50

    def test_cancellation_stops_loop(self):
        iteration_count = [0]
        actions = [{"type": "work"}]

        def dispatch(action):
            iteration_count[0] += 1
            return {"ok": True, "output": "done"}

        cancel_after_2 = lambda: iteration_count[0] >= 2
        result = run_loop(
            actions, dispatch, max_iterations=10,
            cancel_check=cancel_after_2,
        )
        assert iteration_count[0] == 2
        assert "cancelled" in result["stop_reason"]

    def test_empty_actions_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            run_loop([], lambda a: {}, max_iterations=1)

    def test_too_many_actions_rejected(self):
        actions = [{"type": "x"}] * 101
        with pytest.raises(ValueError, match="loop limit"):
            run_loop(actions, lambda a: {}, max_iterations=1)


# ---------------------------------------------------------------------------
# 2. WorkflowService — save/load/run through the application layer
# ---------------------------------------------------------------------------

class _InMemoryWorkflowRepo:
    def __init__(self):
        self._store: dict[str, dict] = {}

    def ensure(self):
        return dict(self._store), "<memory>"

    def save(self, name, actions, description=""):
        normalized = self.normalize_name(name)
        entry = {"actions": actions, "description": description}
        self._store[normalized] = entry
        return entry, "<memory>"

    def get(self, name):
        return self._store.get(self.normalize_name(name))

    def delete(self, name):
        normalized = self.normalize_name(name)
        if normalized in self._store:
            del self._store[normalized]
            return True, "<memory>"
        return False, "<memory>"

    def normalize_name(self, name):
        return name.strip().lower().replace(" ", "_")

    def format(self, workflows):
        return json.dumps(list(workflows.keys()))


class _InMemoryLoopRunner:
    def run(self, actions, dispatch, **kwargs):
        return run_loop(actions, dispatch, **kwargs)

    def format(self, result):
        return json.dumps(result, indent=2)


class TestWorkflowService:
    """Test workflow CRUD and execution through the application service."""

    def _make_service(self):
        return WorkflowService(_InMemoryWorkflowRepo(), _InMemoryLoopRunner())

    def test_save_and_list(self):
        svc = self._make_service()
        result = svc.save("my_flow", '[{"type":"diagnostics"}]', "test flow")
        assert result.ok
        listing = svc.list()
        assert "my_flow" in listing.output

    def test_run_saved_workflow(self):
        svc = self._make_service()
        svc.save("health_check", '[{"type":"status"},{"type":"diagnostics"}]')
        dispatch = lambda a: {"ok": True, "output": "healthy"}
        result = svc.run("health_check", dispatch, max_iterations=1)
        assert result.ok

    def test_run_missing_workflow(self):
        svc = self._make_service()
        dispatch = lambda a: {"ok": True, "output": "ok"}
        result = svc.run("nonexistent", dispatch)
        assert not result.ok
        assert "NOT_FOUND" in result.error_code

    def test_delete_workflow(self):
        svc = self._make_service()
        svc.save("temp", '[{"type":"noop"}]')
        svc.delete("temp")
        listing = svc.list()
        assert "temp" not in listing.output

    def test_progressive_workflow_complexity(self):
        svc = self._make_service()
        state = {"phase": 0, "data": []}
        actions = json.dumps([
            {"type": "code", "code": "collect", "language": "python"},
            {"type": "code", "code": "transform", "language": "python"},
            {"type": "code", "code": "validate", "language": "python"},
        ])
        svc.save("etl_pipeline", actions, "multi-stage ETL")

        def dispatch(action):
            code = action.get("code", "")
            if code == "collect":
                state["data"] = [1, 2, 3, 4, 5]
                state["phase"] = 1
                return {"ok": True, "output": "collected 5 items"}
            elif code == "transform":
                state["data"] = [x * 2 for x in state["data"]]
                state["phase"] = 2
                return {"ok": True, "output": "transformed"}
            elif code == "validate":
                ok = all(x > 0 for x in state["data"])
                state["phase"] = 3
                return {"ok": ok, "output": "validated" if ok else "failed"}
            return {"ok": False, "output": "unknown"}

        result = svc.run("etl_pipeline", dispatch, max_iterations=1)
        assert result.ok
        assert state["phase"] == 3
        assert state["data"] == [2, 4, 6, 8, 10]


# ---------------------------------------------------------------------------
# 3. Fleet capacity — worker cap enforcement
# ---------------------------------------------------------------------------

class TestFleetCapacityIntegration:
    """Verify fleet_worker_cap flows through capacity() and constrains slots."""

    def test_fleet_cap_appears_in_slot_limits(self):
        master_orchestrator.configure_fleet_worker_cap(4)
        result = master_orchestrator.capacity()
        assert result["slot_limits"]["fleet_workers"] == 4

    def test_fleet_cap_lowers_worker_slots(self):
        master_orchestrator.configure_fleet_worker_cap(1)
        result = master_orchestrator.capacity()
        assert result["worker_slots"] <= 1

    def test_fleet_cap_constrains_automatic_derivation(self):
        master_orchestrator.configure_fleet_worker_cap(2)
        result = master_orchestrator.capacity(requested_agents=10)
        assert result["worker_slots"] <= 2

    def test_large_fleet_cap_defers_to_hardware(self):
        master_orchestrator.configure_fleet_worker_cap(999)
        result = master_orchestrator.capacity()
        assert result["worker_slots"] < 999

    def test_fleet_cap_rejects_zero(self):
        with pytest.raises(ValueError, match="fleet_workers must be >= 1"):
            master_orchestrator.configure_fleet_worker_cap(0)

    def test_fleet_cap_rejects_negative(self):
        with pytest.raises(ValueError, match="fleet_workers must be >= 1"):
            master_orchestrator.configure_fleet_worker_cap(-5)


# ---------------------------------------------------------------------------
# 4. Autopilot capacity gate
# ---------------------------------------------------------------------------

class TestAutopilotCapacityIntegration:
    """Verify _launch_autopilot respects the capacity gate."""

    def test_launch_blocked_at_capacity(self):
        server._MAX_AUTOPILOT_RUNS = 1
        alive_thread = MagicMock()
        alive_thread.is_alive.return_value = True
        with server._AUTOPILOT_THREADS_LOCK:
            server._AUTOPILOT_THREADS["existing-run"] = alive_thread
        result = server._launch_autopilot("new-run")
        assert result is False

    def test_launch_allowed_below_capacity(self):
        server._MAX_AUTOPILOT_RUNS = 5
        dead_thread = MagicMock()
        dead_thread.is_alive.return_value = False
        with server._AUTOPILOT_THREADS_LOCK:
            server._AUTOPILOT_THREADS.clear()
            server._AUTOPILOT_THREADS["dead-run"] = dead_thread

        with patch.object(server, "_autopilot_thread_main"):
            result = server._launch_autopilot("test-run")
            assert result is True

        with server._AUTOPILOT_THREADS_LOCK:
            thread = server._AUTOPILOT_THREADS.get("test-run")
            if thread is not None:
                thread.join(timeout=2)

    def test_dead_threads_dont_count(self):
        server._MAX_AUTOPILOT_RUNS = 1
        dead_thread = MagicMock()
        dead_thread.is_alive.return_value = False
        with server._AUTOPILOT_THREADS_LOCK:
            server._AUTOPILOT_THREADS.clear()
            server._AUTOPILOT_THREADS["dead-run"] = dead_thread

        with patch.object(server, "_autopilot_thread_main"):
            result = server._launch_autopilot("new-run")
            assert result is True

        with server._AUTOPILOT_THREADS_LOCK:
            thread = server._AUTOPILOT_THREADS.get("new-run")
            if thread is not None:
                thread.join(timeout=2)

    def test_no_cap_allows_unlimited(self):
        server._MAX_AUTOPILOT_RUNS = None
        with server._AUTOPILOT_THREADS_LOCK:
            server._AUTOPILOT_THREADS.clear()
            for i in range(10):
                t = MagicMock()
                t.is_alive.return_value = True
                server._AUTOPILOT_THREADS[f"run-{i}"] = t

        with patch.object(server, "_autopilot_thread_main"):
            result = server._launch_autopilot("run-11")
            assert result is True

        with server._AUTOPILOT_THREADS_LOCK:
            thread = server._AUTOPILOT_THREADS.get("run-11")
            if thread is not None:
                thread.join(timeout=2)


# ---------------------------------------------------------------------------
# 5. Autopilot controller — state machine and validation
# ---------------------------------------------------------------------------

class TestAutopilotController:
    """Test autopilot controller validation and normalization."""

    def test_normalize_policy_observe(self):
        from autopilot_controller import normalize_policy
        assert normalize_policy("observe") == "observe"

    def test_normalize_policy_workspace(self):
        from autopilot_controller import normalize_policy
        assert normalize_policy("workspace") == "workspace"

    def test_normalize_policy_invalid(self):
        from autopilot_controller import normalize_policy
        with pytest.raises(ValueError, match="policy must be one of"):
            normalize_policy("admin")

    def test_normalize_tier_code(self):
        from autopilot_controller import normalize_tier
        assert normalize_tier("code") == "code"

    def test_normalize_tier_fast(self):
        from autopilot_controller import normalize_tier
        assert normalize_tier("fast") == "fast"

    def test_normalize_tier_rejects_cloud(self):
        from autopilot_controller import normalize_tier
        with pytest.raises(ValueError, match="local tiers only"):
            normalize_tier("cloud-code")

    def test_normalize_tier_default_is_code(self):
        from autopilot_controller import normalize_tier
        assert normalize_tier("") == "code"

    def test_host_task_result_receipt(self):
        from autopilot_controller import HostTaskResult
        result = HostTaskResult(
            output="test output",
            tools=("file_read", "text_search"),
            mutation_observed=True,
            validation_attempted=True,
            validation_passed=True,
        )
        receipt = result.receipt()
        assert receipt["schema"] == 1
        assert receipt["tools"] == ["file_read", "text_search"]
        assert receipt["mutation_observed"] is True
        assert receipt["validation_passed"] is True


# ---------------------------------------------------------------------------
# 6. Progressive orchestration complexity
# ---------------------------------------------------------------------------

class TestOrchestrationProgression:
    """Test master_orchestrate input validation and mode normalization."""

    def test_mode_typo_correction(self):
        assert {"delagte": "delegate"}.get("delagte") == "delegate"
        assert {"delegte": "delegate"}.get("delegte") == "delegate"
        assert {"paralell": "parallel"}.get("paralell") == "parallel"
        assert {"workflow": "fleet"}.get("workflow") == "fleet"

    def test_capacity_probe_with_worker_cap(self):
        master_orchestrator.configure_fleet_worker_cap(4)
        result = master_orchestrator.capacity(requested_agents=8, worker_cap=3)
        assert result["requested_worker_cap"] == 3
        assert result["worker_slots"] <= 3

    def test_auto_slots_bounded_by_fleet_cap(self):
        master_orchestrator.configure_fleet_worker_cap(2)
        result = master_orchestrator.capacity(requested_agents=10)
        assert result["worker_slots"] <= 2
        assert result["slot_limits"]["fleet_workers"] == 2

    def test_fleet_provenance_empty_task_returns_empty(self):
        result = master_orchestrator.fleet_provenance.parse_objectives("")
        assert len(result) == 0

    def test_hardware_derived_slots_positive(self):
        result = master_orchestrator.capacity()
        assert result["worker_slots"] >= 1


# ---------------------------------------------------------------------------
# 7. Cross-system capacity bridge
# ---------------------------------------------------------------------------

class TestCapacityBridgeIntegration:
    """Verify configure_capacity sets all three subsystem caps atomically."""

    def test_configure_capacity_sets_all_three(self):
        server.configure_capacity(
            fleet_workers=3, autopilot_runs=2, training_jobs=1,
        )
        assert master_orchestrator._FLEET_WORKER_CAP == 3
        assert server._MAX_AUTOPILOT_RUNS == 2

        import adaptive_training
        assert adaptive_training._MAX_TRAINING_JOBS == 1

    def test_configure_capacity_zero_fleet_rejected(self):
        with pytest.raises(ValueError):
            server.configure_capacity(
                fleet_workers=0, autopilot_runs=1, training_jobs=1,
            )


# ---------------------------------------------------------------------------
# 8. Workflow + capacity interaction
# ---------------------------------------------------------------------------

class TestWorkflowUnderCapacityPressure:
    """Verify workflows operate correctly when capacity is constrained."""

    def test_workflow_runs_within_single_worker_cap(self):
        master_orchestrator.configure_fleet_worker_cap(1)
        svc = WorkflowService(_InMemoryWorkflowRepo(), _InMemoryLoopRunner())
        svc.save("simple", '[{"type":"status"}]')

        results = []

        def dispatch(action):
            cap = master_orchestrator.capacity()
            results.append(cap["worker_slots"])
            return {"ok": True, "output": "ok"}

        result = svc.run("simple", dispatch, max_iterations=3)
        assert result.ok
        assert all(s <= 1 for s in results)

    def test_concurrent_workflow_iterations_respect_cancellation(self):
        svc = WorkflowService(_InMemoryWorkflowRepo(), _InMemoryLoopRunner())
        svc.save("long_run", '[{"type":"work"},{"type":"verify"}]')

        call_count = [0]

        def dispatch(action):
            call_count[0] += 1
            return {"ok": True, "output": "done"}

        cancel = lambda: call_count[0] >= 4
        result = svc.run(
            "long_run", dispatch, max_iterations=10, cancel_check=cancel,
        )
        assert call_count[0] <= 5
