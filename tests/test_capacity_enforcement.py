"""Tests for CapacityConfig enforcement across fleet, autopilot, and training."""
from __future__ import annotations

import threading
import unittest.mock as mock

import pytest


class TestFleetWorkerCap:
    """Verify _FLEET_WORKER_CAP acts as a ceiling in capacity()."""

    def setup_method(self):
        import master_orchestrator
        self._orig = master_orchestrator._FLEET_WORKER_CAP

    def teardown_method(self):
        import master_orchestrator
        master_orchestrator._FLEET_WORKER_CAP = self._orig

    def test_fleet_worker_cap_lowers_slot_count(self):
        import master_orchestrator
        master_orchestrator.configure_fleet_worker_cap(2)
        result = master_orchestrator.capacity()
        assert "fleet_workers" in result["slot_limits"]
        assert result["slot_limits"]["fleet_workers"] == 2
        assert result["worker_slots"] <= 2

    def test_fleet_worker_cap_appears_in_limits(self):
        import master_orchestrator
        master_orchestrator.configure_fleet_worker_cap(3)
        result = master_orchestrator.capacity()
        assert result["slot_limits"]["fleet_workers"] == 3

    def test_fleet_worker_cap_absent_when_unconfigured(self):
        import master_orchestrator
        master_orchestrator._FLEET_WORKER_CAP = None
        result = master_orchestrator.capacity()
        assert "fleet_workers" not in result["slot_limits"]

    def test_fleet_worker_cap_rejects_zero(self):
        import master_orchestrator
        with pytest.raises(ValueError, match="fleet_workers must be >= 1"):
            master_orchestrator.configure_fleet_worker_cap(0)

    def test_fleet_worker_cap_rejects_negative(self):
        import master_orchestrator
        with pytest.raises(ValueError, match="fleet_workers must be >= 1"):
            master_orchestrator.configure_fleet_worker_cap(-1)


class TestAutopilotCapacity:
    """Verify _MAX_AUTOPILOT_RUNS gates _launch_autopilot()."""

    def setup_method(self):
        import server
        self._orig_cap = server._MAX_AUTOPILOT_RUNS
        self._orig_threads = server._AUTOPILOT_THREADS.copy()

    def teardown_method(self):
        import server
        server._MAX_AUTOPILOT_RUNS = self._orig_cap
        server._AUTOPILOT_THREADS.clear()
        server._AUTOPILOT_THREADS.update(self._orig_threads)

    def test_autopilot_refuses_when_at_capacity(self):
        import server
        server.configure_autopilot_capacity(1)
        alive_thread = mock.MagicMock()
        alive_thread.is_alive.return_value = True
        server._AUTOPILOT_THREADS["existing-run"] = alive_thread
        result = server._launch_autopilot("new-run")
        assert result is False

    def test_autopilot_allows_when_below_capacity(self):
        import server
        server.configure_autopilot_capacity(2)
        alive_thread = mock.MagicMock()
        alive_thread.is_alive.return_value = True
        server._AUTOPILOT_THREADS["existing-run"] = alive_thread

        with mock.patch.object(server, "_autopilot_thread_main"):
            result = server._launch_autopilot("new-run")
        assert result is True

    def test_autopilot_allows_when_dead_threads_dont_count(self):
        import server
        server.configure_autopilot_capacity(1)
        dead_thread = mock.MagicMock()
        dead_thread.is_alive.return_value = False
        server._AUTOPILOT_THREADS["dead-run"] = dead_thread

        with mock.patch.object(server, "_autopilot_thread_main"):
            result = server._launch_autopilot("fresh-run")
        assert result is True

    def test_autopilot_rejects_zero_capacity(self):
        import server
        with pytest.raises(ValueError, match="max_autopilot_runs must be >= 1"):
            server.configure_autopilot_capacity(0)

    def test_autopilot_uncapped_allows_launch(self):
        import server
        server._MAX_AUTOPILOT_RUNS = None
        server._AUTOPILOT_THREADS.clear()
        with mock.patch.object(server, "_autopilot_thread_main"):
            result = server._launch_autopilot("uncapped-run")
        assert result is True


class TestTrainingCapacity:
    """Verify _MAX_TRAINING_JOBS gates start_training()."""

    def setup_method(self):
        import adaptive_training
        self._orig = adaptive_training._MAX_TRAINING_JOBS

    def teardown_method(self):
        import adaptive_training
        adaptive_training._MAX_TRAINING_JOBS = self._orig

    def test_training_blocked_when_zero(self):
        import adaptive_training
        adaptive_training.configure_training_capacity(0)
        plan = mock.MagicMock()
        ok, msg = adaptive_training.start_training(plan, confirmed=True)
        assert ok is False
        assert "blocked" in msg.lower()

    def test_training_dry_run_bypasses_zero_cap(self):
        import adaptive_training
        adaptive_training.configure_training_capacity(0)
        plan = mock.MagicMock()
        plan.training.enabled = False
        with mock.patch.object(adaptive_training, "format_plan", return_value="(dry)"):
            ok, msg = adaptive_training.start_training(plan, dry_run=True)
        assert ok is True
        assert "blocked" not in msg.lower()

    def test_training_rejects_negative(self):
        import adaptive_training
        with pytest.raises(ValueError, match="training_jobs must be >= 0"):
            adaptive_training.configure_training_capacity(-1)


class TestConfigureCapacityBridge:
    """Verify server.configure_capacity delegates to all three modules."""

    def test_configure_capacity_sets_all_caps(self):
        import server
        import master_orchestrator
        import adaptive_training

        orig_auto = server._MAX_AUTOPILOT_RUNS
        orig_fleet = master_orchestrator._FLEET_WORKER_CAP
        orig_train = adaptive_training._MAX_TRAINING_JOBS

        try:
            server.configure_capacity(
                autopilot_runs=3,
                fleet_workers=5,
                training_jobs=2,
            )
            assert server._MAX_AUTOPILOT_RUNS == 3
            assert master_orchestrator._FLEET_WORKER_CAP == 5
            assert adaptive_training._MAX_TRAINING_JOBS == 2
        finally:
            server._MAX_AUTOPILOT_RUNS = orig_auto
            master_orchestrator._FLEET_WORKER_CAP = orig_fleet
            adaptive_training._MAX_TRAINING_JOBS = orig_train
