"""The real interactive lane records only committed host outcomes."""

import sqlite3

import pytest

from sonder_runtime.adapters.persistence.agent_lanes import SQLiteAgentLaneStore
from sonder_runtime.adapters.persistence.session_repository import (
    SQLiteSessionRepository,
)
from sonder_runtime.adapters.unit_of_work import UnitOfWorkAdapter
from sonder_runtime.application.agents.interactive_lanes import AgentLaneService
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.memory.strategy_memory import StrategyMemoryService
from sonder_runtime.application.ports.model_gateway import ModelResponse
from sonder_runtime.bootstrap.app import build_application
from sonder_runtime.bootstrap.strategy import (
    StrategyRollout,
    compose_strategy_trace,
    compose_workbench_strategy_observer,
)
from sonder_runtime.domain.strategy.models import FailureClass


def test_production_application_composes_workbench_observer(monkeypatch, tmp_path):
    monkeypatch.setenv("SONDER_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SONDER_STRATEGY_MODE", "observe")
    application = build_application()
    try:
        assert application.agent_lanes()._strategy_observer is not None
    finally:
        application.close_providers(timeout=5)


class Model:
    def __init__(self, failure=None):
        self.failure = failure
        self.calls = 0

    def generate(self, request, context):
        self.calls += 1
        if self.failure is not None:
            raise self.failure
        return ModelResponse("A completed answer", "fake", request.tier, tokens_out=4)


def setup(tmp_path, *, observe=True, failure=None, mode="observe"):
    sessions = SQLiteSessionRepository(tmp_path / "sessions.db")
    store = SQLiteAgentLaneStore(tmp_path / "lanes.db", sessions)
    trace = compose_strategy_trace(
        db_path=tmp_path / "strategy.db",
        key_path=tmp_path / "private" / "seal.key",
    )
    memory_path = tmp_path / "memory.db"
    memory = StrategyMemoryService(
        trace, lambda: UnitOfWorkAdapter(str(memory_path)),
    )
    observer = compose_workbench_strategy_observer(
        trace, memory, StrategyRollout(mode, 100), store,
    ) if observe else None
    model = Model(failure)
    service = AgentLaneService(
        store, sessions, model, auto_start=False, strategy_observer=observer,
    )
    context = local_owner_context(
        correlation_id="strategy-test", workspace_roots=(tmp_path,),
    )
    workspace = tmp_path / "child"
    workspace.mkdir()
    lane_id = service.spawn(
        command_id="spawn-1", parent_session_id="parent", task="implement parser",
        workspace_root=str(workspace), context=context,
    )["lane"]["id"]
    return service, store, sessions, model, context, trace, memory, memory_path, lane_id


def test_completed_workbench_lane_is_observed_and_indexed(tmp_path):
    service, store, _sessions, model, context, trace, _memory, memory_path, lane_id = setup(tmp_path)

    service.run_pending(lane_id, context)

    assert model.calls == 1
    assert store.read_lane(lane_id)["status"] == "completed"
    history = compose_strategy_trace(
        db_path=tmp_path / "strategy.db",
        key_path=tmp_path / "private" / "seal.key",
    ).history(lane_id)
    assert len(history) == 1 and history[0].outcome == "succeeded"
    assert history[0].usage.attempts == 1
    assert history[0].usage.model_calls == model.calls == store.read_lane(lane_id)["used_steps"]
    with sqlite3.connect(memory_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM strategy_experience").fetchone()[0] == 1
    service.inspect(lane_id, context)
    assert len(trace.history(lane_id)) == 1


def test_failed_workbench_lane_backfills_after_observer_restart(tmp_path):
    service, store, sessions, model, context, trace, memory, _memory_path, lane_id = setup(
        tmp_path, observe=False, failure=OSError("connection lost after dispatch"),
    )
    service.run_pending(lane_id, context)
    assert store.read_lane(lane_id)["status"] == "awaiting_input"
    assert trace.history(lane_id) == ()

    observer = compose_workbench_strategy_observer(
        trace, memory, StrategyRollout("shadow"), store,
    )
    reopened = AgentLaneService(
        store, sessions, model, auto_start=False, strategy_observer=observer,
    )
    reopened.inspect(lane_id, context)

    history = compose_strategy_trace(
        db_path=tmp_path / "strategy.db",
        key_path=tmp_path / "private" / "seal.key",
    ).history(lane_id)
    assert len(history) == 1
    assert history[0].outcome == "uncertain"
    assert history[0].failure.classification is FailureClass.UNCERTAIN_SIDE_EFFECT
    assert history[0].progress_after.complete is False
    events, _more = store.events(lane_id, 0, 100)
    comparison = [event for event in events
                  if event["event_type"] == "strategy.shadow"]
    assert len(comparison) == 1
    assert comparison[0]["payload"]["legacy_action"] == "reconcile"
    assert comparison[0]["payload"]["applied"] is False
    with pytest.raises(ValueError, match="uncertain attempt needs reconciliation"):
        reopened.control(lane_id, "resume", command_id="resume", context=context)
    assert model.calls == 1
    assert len(trace.history(lane_id)) == 1
