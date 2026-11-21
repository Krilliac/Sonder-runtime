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


def _launch(service, agent_id, root, **kw):
    return service.fleet.launch_fleet(
        agent_id=agent_id, operation_id="o-" + agent_id, prompt="p",
        idempotency_key="i-" + agent_id, budget=kw.pop("b", budget(max_concurrency=1)),
        lineage=AgentLineage(root), **kw,
    )


def test_cancel_releases_only_the_cancelled_agents_root():
    service = UnifiedAgentRegistryService(Backend())
    _launch(service, "A", "rootA")
    _launch(service, "B", "rootB")
    service.fleet.cancel("B")
    with pytest.raises(AgentRegistryError, match="concurrency"):
        _launch(service, "A2", "rootA")
    _launch(service, "B2", "rootB")


def test_repeated_cancel_and_stop_settle_once():
    backend = Backend()
    service = UnifiedAgentRegistryService(backend)
    b = budget(max_concurrency=2)
    _launch(service, "A", "rootA", b=b)
    _launch(service, "A2", "rootA", b=b)
    service.fleet.cancel("A")
    service.fleet.cancel("A")
    backend.records["A"]["status"] = "stopped"
    service.stop("A")
    assert service._active == {"rootA": 1}


def test_cancel_of_unknown_or_unadmitted_agent_changes_no_reservation():
    backend = Backend()
    backend.records["ghost"] = {"id": "ghost", "status": "cancelled"}
    service = UnifiedAgentRegistryService(backend)
    _launch(service, "C", "rootC")
    service.fleet.cancel("ghost")
    assert service._active == {"rootC": 1}


def test_non_terminal_ack_keeps_reservation():
    backend = Backend()
    service = UnifiedAgentRegistryService(backend)
    _launch(service, "A", "rootA")
    backend.cancel = lambda agent_id, *, reason="": {"id": agent_id, "status": "running"}
    service.fleet.cancel("A")
    assert service._active == {"rootA": 1}


def test_recreate_same_agent_takes_no_second_reservation():
    service = UnifiedAgentRegistryService(Backend())
    _launch(service, "A", "rootA")
    _launch(service, "A", "rootA")
    assert service._active == {"rootA": 1}


def test_child_budget_released_on_settle():
    service = UnifiedAgentRegistryService(Backend())
    b = budget(max_children=1, max_concurrency=3)
    _launch(service, "p", "root", b=b)
    _launch(service, "c1", "root", b=b, parent_id="p")
    with pytest.raises(AgentRegistryError, match="child"):
        _launch(service, "c2", "root", b=b, parent_id="p")
    service.fleet.cancel("c1")
    _launch(service, "c2", "root", b=b, parent_id="p")


def _real_service():
    from sonder_runtime.adapters.persistence.fleet_registry import FleetStoreRegistryAdapter

    return UnifiedAgentRegistryService(FleetStoreRegistryAdapter())


def test_real_adapter_cancel_of_queued_agent_frees_exactly_its_own_root():
    service = _real_service()
    _launch(service, "A", "rootA")
    _launch(service, "B", "rootB")
    service.fleet.cancel("B")
    assert service._active == {"rootA": 1}
    with pytest.raises(AgentRegistryError, match="concurrency"):
        _launch(service, "A2", "rootA")
    service.fleet.cancel("B")  # repeated cancel settles nothing more
    assert service._active == {"rootA": 1}
    service.fleet.cancel("A")
    assert service._active == {}
    _launch(service, "A3", "rootA")


def test_real_adapter_parent_cancel_settles_queued_child_and_child_budget():
    service = _real_service()
    b = budget(max_children=1, max_concurrency=3)
    _launch(service, "P", "root", b=b)
    _launch(service, "C", "root", b=b, parent_id="P")
    service.fleet.cancel("P")
    assert service._active == {}
    assert service._children == {}
    assert service._receipts == {}


def test_real_adapter_running_row_with_cancel_requested_keeps_reservation():
    from sonder_runtime.adapters.persistence import fleet_store

    service = _real_service()
    _launch(service, "R", "rootR")
    conn = fleet_store._connect()
    try:
        conn.execute("UPDATE fleet_agents SET status='running' WHERE id='R'")
        conn.commit()
    finally:
        conn.close()
    service.fleet.cancel("R")
    assert service.status("R")["cancel_requested"] is True
    assert service._active == {"rootR": 1}


def test_real_adapter_child_created_terminal_under_cancelled_parent_releases_reservation():
    from sonder_runtime.adapters.persistence import fleet_store

    service = _real_service()
    b = budget(max_children=2, max_concurrency=3)
    _launch(service, "P", "root", b=b)
    conn = fleet_store._connect()
    try:
        conn.execute("UPDATE fleet_agents SET status='running' WHERE id='P'")
        conn.commit()
    finally:
        conn.close()
    service.fleet.cancel("P")
    assert service._active == {"root": 1}
    _launch(service, "C", "root", b=b, parent_id="P")
    assert service.status("C")["status"] == "cancelled"
    assert service._active == {"root": 1}
    assert service._children == {}
    assert set(service._receipts) == {"P"}
    service.fleet.cancel("C")
    assert service._active == {"root": 1}
