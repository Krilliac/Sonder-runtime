from __future__ import annotations

from sonder_runtime.adapters.web.lifecycle import RuntimeLifecycle
from sonder_runtime.application.operations.graceful_drain import (
    DrainStage,
    GracefulDrainCoordinator,
)


class _Admission:
    def stop_admission(self, reason):
        return True


class _Descendants:
    def cancel_descendants(self, reason):
        return True

    def settle_descendants(self, deadline_monotonic):
        return True


class _Deadline:
    def announce_deadline(self, notice):
        return True


class _ProcessTree:
    def cleanup(self, request):
        raise AssertionError("no process cleanup should run without observations")


def _coordinator():
    return GracefulDrainCoordinator(
        admission=_Admission(),
        descendants=_Descendants(),
        deadline_communicator=_Deadline(),
        flush=lambda remaining: True,
        cleanup=lambda remaining: True,
        process_tree=_ProcessTree(),
        clock=lambda: 0.0,
    )


def test_unconfigured_bridge_is_truthfully_incomplete_and_legacy_surface_remains(monkeypatch):
    monkeypatch.setattr(
        "sonder_runtime.adapters.web.lifecycle._build_production_graceful_drain_bridge",
        lambda _lifecycle: None,
    )
    lifecycle = RuntimeLifecycle()

    result = lifecycle.drain_gracefully("not wired")

    assert result.stage is DrainStage.INCOMPLETE
    assert not result.clean
    assert "bridge is not configured" in result.errors[0]


def test_default_production_bridge_is_complete_after_startup_without_jobs(tmp_path, monkeypatch):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    lifecycle = RuntimeLifecycle(startup_reconciler=lambda: 0)
    lifecycle.startup(run_migrations=False)

    result = lifecycle.drain_gracefully("default bridge")

    assert result.clean
    assert result.stage is DrainStage.COMPLETE
    assert lifecycle.coordinator.cancellation.cancelled


def test_injected_bridge_runs_graceful_coordinator_and_observation_provider():
    seen = []
    lifecycle = RuntimeLifecycle(
        graceful_drain_coordinator=_coordinator(),
        graceful_drain_observations=lambda: seen.append("observed") or (),
    )

    result = lifecycle.drain_gracefully("injected")

    assert result.clean
    assert result.stage is DrainStage.COMPLETE
    assert seen == ["observed"]


def test_injected_bridge_failure_is_truthful_and_does_not_raise():
    class Broken:
        def drain(self, request, *, observations):
            raise RuntimeError("boom")

    lifecycle = RuntimeLifecycle(graceful_drain_coordinator=Broken())

    result = lifecycle.drain_gracefully("broken")

    assert result.stage is DrainStage.INCOMPLETE
    assert not result.clean
    assert result.errors == ("graceful drain bridge: RuntimeError",)


def test_legacy_drain_surface_uses_production_bridge_and_stops_cleanly(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path))
    lifecycle = RuntimeLifecycle(startup_reconciler=lambda: 0)
    lifecycle.startup(run_migrations=False)

    assert lifecycle.drain("admin request") is True
    assert lifecycle.tracker.snapshot().process.value == "stopping"
