"""Production Fleet strategy observation and bounded canary retry tests."""
from types import SimpleNamespace

import pytest

import master_orchestrator
import server
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.bootstrap.strategy import compose_strategy_trace
from sonder_runtime.domain.strategy.models import FailureClass


@pytest.fixture
def fleet_state(monkeypatch, tmp_path):
    import sonder_runtime.adapters.persistence.fleet_store as fleet_store

    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SONDER_FLEET_DB", str(tmp_path / "fleet.db"))
    monkeypatch.setenv("SONDER_FLEET_TRANSIENT_RETRIES", "3")
    monkeypatch.setattr(
        server, "_application",
        lambda: SimpleNamespace(
            unit_of_work=lambda: UnitOfWorkAdapter(str(tmp_path / "memory.db")),
        ),
    )
    monkeypatch.setattr(server.creative_router, "classify", lambda *args, **kwargs: None)
    master_orchestrator.reset_for_tests()
    fleet_store.reset_schema_cache_for_tests()
    fleet_store.clear_all()
    return tmp_path


def _child_row():
    return next(
        row for row in master_orchestrator.snapshot()["agents"]
        if row["role"] == "agent"
    )


@pytest.mark.parametrize(
    ("mode", "expected_attempts"),
    [("observe", 4), ("shadow", 4), ("canary", 2)],
)
def test_production_pure_worker_records_failure_before_retry_and_restarts(
    fleet_state, monkeypatch, mode, expected_attempts,
):
    monkeypatch.setenv("SONDER_STRATEGY_MODE", mode)
    monkeypatch.setenv("SONDER_STRATEGY_CANARY_PERCENT", "100")
    calls = []

    def failing_worker(_tier, **_options):
        def run(_prompt):
            calls.append(1)
            if len(calls) == 2:
                child = _child_row()
                first = compose_strategy_trace().history(child["id"])
                assert len(first) == 1
                assert first[0].failure.classification is FailureClass.TRANSIENT_TRANSPORT
                assert first[0].usage.attempts == 1
            raise TimeoutError("model call timed out")

        return run

    monkeypatch.setattr(server, "_orchestrator_worker", failing_worker)
    result = server.master_orchestrate(
        "compare options", mode="delegate", agents=1, tier="fast", learn=False,
    )

    assert "all delegated workers failed" in result
    assert len(calls) == expected_attempts
    child = _child_row()
    assert child["status"] == "failed"
    restarted = compose_strategy_trace()
    history = restarted.history(child["id"])
    assert len(history) == expected_attempts
    assert [entry.usage.attempts for entry in history] == [1] * expected_attempts
    if mode in {"shadow", "canary"}:
        assert any("strategy shadow:" in event["message"] for event in
                   master_orchestrator.snapshot()["events"])


def test_canary_does_not_control_unproven_repository_worker(fleet_state, monkeypatch):
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "canary")
    monkeypatch.setenv("SONDER_STRATEGY_CANARY_PERCENT", "100")
    calls = []

    def failing_worker(_tier, _project, **_options):
        def run(_prompt, _assigned_project):
            calls.append(1)
            raise TimeoutError("model call timed out")

        return run

    monkeypatch.setattr(server, "_orchestrator_agent_worker", failing_worker)
    result = server.master_orchestrate(
        "compare these files", mode="delegate", agents=1, tier="fast",
        project=str(fleet_state),
    )

    assert len(calls) == 4
    assert "all delegated workers failed" in result or "EVIDENCE" in result
    child = _child_row()
    history = compose_strategy_trace().history(child["id"])
    assert len(history) == 4
    assert all(entry.outcome == "uncertain" for entry in history)


def test_selected_pure_canary_suppresses_retry_if_seal_unavailable(fleet_state, monkeypatch):
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "canary")
    monkeypatch.setenv("SONDER_STRATEGY_CANARY_PERCENT", "100")
    monkeypatch.setenv("SONDER_STRATEGY_CHECKPOINT_DB", str(fleet_state / "fleet.db"))
    calls = []

    def failing_worker(_tier, **_options):
        def run(_prompt):
            calls.append(1)
            raise TimeoutError("model call timed out")

        return run

    monkeypatch.setattr(server, "_orchestrator_worker", failing_worker)
    result = server.master_orchestrate(
        "compare options", mode="delegate", agents=1, tier="fast", learn=False,
    )

    assert len(calls) == 1
    assert "all delegated workers failed" in result
