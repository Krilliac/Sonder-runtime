"""Tests for the four NVIDIA-inspired fleet patterns.

1. EWMA pressure banding with hysteresis
2. Optimistic reservation counters
3. Convergent snapshot pub-sub
4. Forward-only state merge with provenance
"""
from __future__ import annotations

import threading
import time

import pytest

from sonder_runtime.domain import fleet_pressure
from sonder_runtime.domain.fleet_pressure import (
    BAND_CRITICAL,
    BAND_HIGH,
    BAND_LOW,
    BAND_MEDIUM,
    PressureTracker,
    admission_factor,
)
from sonder_runtime.domain.automation.state_machine import (
    fleet_can_transition,
    FLEET_ACTIVE,
    FLEET_TERMINAL,
)


# ---------------------------------------------------------------------------
# 1. EWMA pressure banding with hysteresis
# ---------------------------------------------------------------------------

class TestPressureTracker:

    def test_initial_state_is_low(self):
        t = PressureTracker()
        assert t.band == BAND_LOW
        assert t.ewma == 0.0
        assert t.sample_count == 0

    def test_first_sample_seeds_ewma(self):
        t = PressureTracker()
        s = t.update(5, 10, ts=1.0)
        assert s.ewma == 0.5
        assert s.raw == 0.5
        assert s.capacity == 10
        assert t.sample_count == 1

    def test_ewma_blends_subsequent_samples(self):
        t = PressureTracker(alpha=0.5)
        t.update(10, 10, ts=1.0)
        s = t.update(0, 10, ts=2.0)
        assert s.ewma == 0.5
        s = t.update(0, 10, ts=3.0)
        assert s.ewma == 0.25

    def test_band_rises_on_sustained_load(self):
        t = PressureTracker(alpha=0.8)
        for _ in range(10):
            t.update(9, 10, ts=time.monotonic())
        assert t.band in (BAND_HIGH, BAND_CRITICAL)

    def test_hysteresis_prevents_oscillation(self):
        t = PressureTracker(
            alpha=1.0,
            thresholds=((0.5, 0.3),),
        )
        t.update(6, 10, ts=1.0)
        assert t.band == BAND_MEDIUM

        t.update(4, 10, ts=2.0)
        assert t.band == BAND_MEDIUM, "should not drop: 0.4 > down threshold 0.3"

        t.update(2, 10, ts=3.0)
        assert t.band == BAND_LOW, "should drop: 0.2 < down threshold 0.3"

    def test_capacity_clamped_to_at_least_one(self):
        t = PressureTracker()
        s = t.update(5, 0, ts=1.0)
        assert s.capacity == 1

    def test_utilization_capped_at_one(self):
        t = PressureTracker()
        s = t.update(20, 10, ts=1.0)
        assert s.utilization == 1.0

    def test_current_returns_latest_state(self):
        t = PressureTracker()
        t.update(3, 10, ts=1.0)
        s = t.current()
        assert s.ewma == t.ewma
        assert s.band == t.band

    def test_multiple_threshold_bands(self):
        t = PressureTracker(
            alpha=1.0,
            thresholds=((0.4, 0.3), (0.7, 0.6), (0.9, 0.85)),
        )
        t.update(5, 10, ts=1.0)
        assert t.band == BAND_MEDIUM
        t.update(8, 10, ts=2.0)
        assert t.band == BAND_HIGH
        t.update(10, 10, ts=3.0)
        assert t.band == BAND_CRITICAL

    def test_band_descent_requires_each_down_threshold(self):
        t = PressureTracker(
            alpha=1.0,
            thresholds=((0.4, 0.3), (0.7, 0.6), (0.9, 0.85)),
        )
        for _ in range(3):
            t.update(10, 10, ts=time.monotonic())
        assert t.band == BAND_CRITICAL
        t.update(8, 10, ts=time.monotonic())
        assert t.band == BAND_HIGH
        t.update(5, 10, ts=time.monotonic())
        assert t.band == BAND_MEDIUM


class TestAdmissionFactor:

    def test_low_admits_fully(self):
        assert admission_factor(BAND_LOW) == 1.0

    def test_critical_sheds_all(self):
        assert admission_factor(BAND_CRITICAL) == 0.0

    def test_medium_and_high_are_intermediate(self):
        assert 0.0 < admission_factor(BAND_MEDIUM) < 1.0
        assert 0.0 < admission_factor(BAND_HIGH) < 1.0
        assert admission_factor(BAND_MEDIUM) > admission_factor(BAND_HIGH)

    def test_unknown_band_defaults_to_full(self):
        assert admission_factor("unknown") == 1.0


# ---------------------------------------------------------------------------
# 2. Forward-only state merge with provenance
# ---------------------------------------------------------------------------

class TestForwardOnlyMerge:

    def test_valid_forward_transitions(self):
        assert fleet_can_transition("queued", "running")
        assert fleet_can_transition("running", "done")
        assert fleet_can_transition("running", "failed")
        assert fleet_can_transition("running", "cancelled")

    def test_backward_transitions_rejected(self):
        assert not fleet_can_transition("done", "running")
        assert not fleet_can_transition("failed", "running")
        assert not fleet_can_transition("done", "queued")

    def test_terminal_states_are_absorbing(self):
        for terminal in FLEET_TERMINAL:
            if terminal in ("interrupted", "failed", "task_drift", "cancelled"):
                continue
            assert not fleet_can_transition(terminal, "running")
            assert not fleet_can_transition(terminal, "queued")

    def test_retryable_states_can_requeue(self):
        for status in ("interrupted", "failed", "task_drift", "cancelled"):
            assert fleet_can_transition(status, "queued")
            assert fleet_can_transition(status, "retried")

    def test_unknown_states_rejected(self):
        assert not fleet_can_transition("unknown", "running")
        assert not fleet_can_transition("running", "unknown")


class TestTransitionProvenance:

    def test_provenance_valid_transition(self):
        from sonder_runtime.adapters.persistence.fleet_store import transition_provenance
        prov = transition_provenance("queued", "running", source="process")
        assert prov["from"] == "queued"
        assert prov["to"] == "running"
        assert prov["source"] == "process"
        assert prov["valid"] is True
        assert "ts" in prov

    def test_provenance_invalid_transition(self):
        from sonder_runtime.adapters.persistence.fleet_store import transition_provenance
        prov = transition_provenance("done", "running", source="stale_reconciliation")
        assert prov["valid"] is False
        assert prov["source"] == "stale_reconciliation"


# ---------------------------------------------------------------------------
# 3. Optimistic reservation counters (integration with master_orchestrator)
# ---------------------------------------------------------------------------

class TestOptimisticReservation:

    def test_reserved_slots_starts_at_zero(self):
        import master_orchestrator as mo
        with mo._LOCK:
            initial = mo._RESERVED_SLOTS
        assert initial >= 0

    def test_capacity_includes_reserved_key_when_nonzero(self):
        import master_orchestrator as mo
        with mo._LOCK:
            original = mo._RESERVED_SLOTS
            mo._RESERVED_SLOTS = 3
        try:
            cap = mo.capacity(requested_agents=10)
            assert "reserved_capacity" in cap["slot_limits"]
            assert cap["slot_limits"]["reserved_capacity"] <= 10
        finally:
            with mo._LOCK:
                mo._RESERVED_SLOTS = original

    def test_capacity_omits_reserved_when_zero(self):
        import master_orchestrator as mo
        with mo._LOCK:
            original = mo._RESERVED_SLOTS
            mo._RESERVED_SLOTS = 0
        try:
            cap = mo.capacity(requested_agents=10)
            assert "reserved_capacity" not in cap["slot_limits"]
        finally:
            with mo._LOCK:
                mo._RESERVED_SLOTS = original

    def test_reserved_capacity_floor_is_one(self):
        import master_orchestrator as mo
        with mo._LOCK:
            original = mo._RESERVED_SLOTS
            mo._RESERVED_SLOTS = 100
        try:
            cap = mo.capacity(requested_agents=5)
            assert cap["slot_limits"]["reserved_capacity"] >= 1
        finally:
            with mo._LOCK:
                mo._RESERVED_SLOTS = original


# ---------------------------------------------------------------------------
# 4. Convergent snapshot pub-sub
# ---------------------------------------------------------------------------

class TestSnapshotPubSub:

    def test_subscribe_and_unsubscribe(self):
        import master_orchestrator as mo
        received = []

        def on_snap(snap, agent_id, event):
            received.append((agent_id, event))

        mo.subscribe_fleet_snapshots(on_snap)
        with mo._LOCK:
            assert on_snap in mo._SNAPSHOT_SUBSCRIBERS
        mo.unsubscribe_fleet_snapshots(on_snap)
        with mo._LOCK:
            assert on_snap not in mo._SNAPSHOT_SUBSCRIBERS

    def test_unsubscribe_nonexistent_is_safe(self):
        import master_orchestrator as mo

        def dummy(snap, aid, ev):
            pass

        mo.unsubscribe_fleet_snapshots(dummy)

    def test_notify_delivers_snapshot_to_subscribers(self):
        import master_orchestrator as mo
        received = []

        def on_snap(snap, agent_id, event):
            received.append({
                "agent_id": agent_id,
                "event": event,
                "has_active": "active_agents" in snap,
                "has_pressure": "pressure" in snap,
            })

        mo.subscribe_fleet_snapshots(on_snap)
        try:
            mo._notify_snapshot_subscribers("test-agent-001", "test")
            assert len(received) == 1
            assert received[0]["agent_id"] == "test-agent-001"
            assert received[0]["event"] == "test"
            assert received[0]["has_active"]
            assert received[0]["has_pressure"]
        finally:
            mo.unsubscribe_fleet_snapshots(on_snap)

    def test_subscriber_exception_does_not_propagate(self):
        import master_orchestrator as mo

        def bad_sub(snap, aid, ev):
            raise RuntimeError("subscriber crash")

        received = []

        def good_sub(snap, aid, ev):
            received.append(ev)

        mo.subscribe_fleet_snapshots(bad_sub)
        mo.subscribe_fleet_snapshots(good_sub)
        try:
            mo._notify_snapshot_subscribers("agent-x", "finish")
            assert len(received) == 1
        finally:
            mo.unsubscribe_fleet_snapshots(bad_sub)
            mo.unsubscribe_fleet_snapshots(good_sub)

    def test_snapshot_includes_pressure_band(self):
        import master_orchestrator as mo
        received = []

        def on_snap(snap, aid, ev):
            received.append(snap.get("pressure", {}))

        mo.subscribe_fleet_snapshots(on_snap)
        try:
            mo._notify_snapshot_subscribers("agent-y", "start")
            assert len(received) == 1
            pressure = received[0]
            assert "ewma" in pressure
            assert "band" in pressure
            assert pressure["band"] in fleet_pressure.BANDS
        finally:
            mo.unsubscribe_fleet_snapshots(on_snap)


class TestFleetPressureAPI:

    def test_fleet_pressure_band_returns_string(self):
        import master_orchestrator as mo
        band = mo.fleet_pressure_band()
        assert band in fleet_pressure.BANDS

    def test_fleet_pressure_sample_returns_dataclass(self):
        import master_orchestrator as mo
        sample = mo.fleet_pressure_sample()
        assert hasattr(sample, "ewma")
        assert hasattr(sample, "band")
        assert sample.band in fleet_pressure.BANDS

    def test_reserved_slot_count_returns_int(self):
        import master_orchestrator as mo
        count = mo.reserved_slot_count()
        assert isinstance(count, int)
        assert count >= 0
