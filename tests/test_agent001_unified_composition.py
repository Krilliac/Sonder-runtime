"""AGENT-001: one bounded Fleet/Autopilot/Workbench composition path."""
from __future__ import annotations

import pytest

from sonder_runtime.application.agent_registry.fleet_autopilot import (
    AgentBudget, AgentLineage, AgentMode, AgentRegistryError,
)
from sonder_runtime.application.agent_registry.unified import UnifiedAgentRegistryService


class Backend:
    def __init__(self):
        self.records = {}
        self.calls = []

    def create(self, launch):
        self.calls.append(("create", launch))
        if launch.agent_id in self.records:
            return self.records[launch.agent_id]
        record = {"id": launch.agent_id, "status": "queued", "parent_id": launch.parent_id}
        self.records[launch.agent_id] = record
        return record

    def status(self, agent_id):
        return self.records[agent_id]

    def resume(self, agent_id):
        self.calls.append(("resume", agent_id))
        return {**self.records[agent_id], "restart_required": True}

    def cancel(self, agent_id, *, reason=""):
        self.records[agent_id]["status"] = "cancelled"
        return self.records[agent_id]

    def stop(self, agent_id, *, reason=""):
        self.records[agent_id]["status"] = "interrupted"
        return self.records[agent_id]


def budget(**overrides):
    values = dict(max_steps=4, max_output_tokens=100, max_wall_seconds=10,
                  max_children=2, max_depth=2, max_concurrency=2)
    values.update(overrides)
    return AgentBudget(**values)


def test_all_typed_modes_share_one_backend_and_preserve_admission_metadata():
    backend = Backend()
    service = UnifiedAgentRegistryService(backend)
    service.register_workbench_modes()

    service.fleet.launch_fleet(
        agent_id="fleet-1", operation_id="op-1", prompt="inspect",
        idempotency_key="idem-1", budget=budget(),
        lineage=AgentLineage("fleet-1"), metadata={"role": "worker"},
    )
    service.fleet.launch_autopilot(
        agent_id="auto-1", operation_id="op-2", prompt="continue",
        idempotency_key="idem-2", parent_id="fleet-1", budget=budget(),
        lineage=AgentLineage("fleet-1", 1),
    )
    invocation = service.invoke_workbench("review", "inspect", correlation_id="c-1")

    assert [call[1].mode for call in backend.calls] == [AgentMode.FLEET, AgentMode.AUTOPILOT]
    assert backend.calls[1][1].lineage == AgentLineage("fleet-1", 1)
    assert invocation.registration.mutation_policy == "read_only"


def test_admission_fails_closed_for_missing_budget_depth_and_concurrency():
    service = UnifiedAgentRegistryService(Backend())
    with pytest.raises(AgentRegistryError, match="explicit budget"):
        service.fleet.launch_fleet(agent_id="a", operation_id="o", prompt="p", idempotency_key="i")
    with pytest.raises(AgentRegistryError, match="depth"):
        service.fleet.launch_fleet(
            agent_id="a", operation_id="o", prompt="p", idempotency_key="i",
            budget=budget(max_depth=1), lineage=AgentLineage("a", 2),
        )
    service.fleet.launch_fleet(
        agent_id="a", operation_id="o", prompt="p", idempotency_key="i",
        budget=budget(max_concurrency=1), lineage=AgentLineage("a"),
    )
    with pytest.raises(AgentRegistryError, match="concurrency"):
        service.fleet.launch_autopilot(
            agent_id="b", operation_id="o2", prompt="p", idempotency_key="i2",
            budget=budget(max_concurrency=1), lineage=AgentLineage("a"),
        )


def test_resume_requires_durable_restart_truth_and_cancel_releases_reservation():
    backend = Backend()
    service = UnifiedAgentRegistryService(backend)
    service.fleet.launch_fleet(
        agent_id="a", operation_id="o", prompt="p", idempotency_key="i",
        budget=budget(max_concurrency=1), lineage=AgentLineage("a"),
    )
    with pytest.raises(AgentRegistryError, match="restart truth"):
        service.fleet.resume("a")
    service.fleet.cancel("a", reason="operator")
    service.fleet.launch_fleet(
        agent_id="b", operation_id="o2", prompt="p", idempotency_key="i2",
        budget=budget(max_concurrency=1), lineage=AgentLineage("b"),
    )
    backend.records["b"]["status"] = "interrupted"
    result = service.fleet.resume("b")
    assert result["restart_required"] is True


def test_application_exposes_lazy_unified_registry_factory():
    from sonder_runtime.bootstrap.app import build_application

    app = build_application()
    assert app.agent_registry is not None
    assert app.agent_registry() is app.agent_registry()
