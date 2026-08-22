"""Small unified Fleet/Autopilot/Workbench composition boundary (AGENT-001)."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from .fleet_autopilot import (
    AgentBudget, AgentLaunch, AgentLineage, AgentMode, AgentRegistryError,
    FleetAutopilotAdapter,
)
from .workbench_review import AgentInvocation, AgentRegistration, WorkbenchReviewAdapter


class UnifiedRegistryBackend(Protocol):
    def create(self, launch: AgentLaunch) -> Any: ...
    def resume(self, agent_id: str) -> Any: ...
    def cancel(self, agent_id: str, *, reason: str = "") -> Any: ...
    def stop(self, agent_id: str, *, reason: str = "") -> Any: ...
    def status(self, agent_id: str) -> Any: ...


@dataclass(frozen=True)
class AdmissionReceipt:
    agent_id: str
    root_id: str
    depth: int
    budget: AgentBudget


class UnifiedAgentRegistryService:
    """Compose the typed mode adapters behind one fail-closed admission gate.

    The backend owns durable state and restart truth.  This service owns only
    admission reservations and the shared mode vocabulary; it never invents
    a restart or bypasses the backend's status check.
    """

    def __init__(self, backend: UnifiedRegistryBackend) -> None:
        self._backend = backend
        self._fleet = FleetAutopilotAdapter(self)
        self._workbench = WorkbenchReviewAdapter()
        self._registrations: dict[str, AgentRegistration] = {}
        self._active: dict[str, int] = {}
        self._children: dict[str, int] = {}

    @property
    def fleet(self) -> FleetAutopilotAdapter:
        return self._fleet

    @property
    def workbench(self) -> WorkbenchReviewAdapter:
        return self._workbench

    @property
    def registrations(self) -> tuple[AgentRegistration, ...]:
        return tuple(self._registrations.values())

    def register_workbench_modes(self) -> tuple[AgentRegistration, ...]:
        for registration in self._workbench.registrations:
            self._registrations[registration.name] = registration
        return self._workbench.register(self)

    def register(self, registration: AgentRegistration) -> AgentRegistration:
        if registration.name in self._registrations:
            return self._registrations[registration.name]
        self._registrations[registration.name] = registration
        return registration

    def invoke_workbench(self, name: str, prompt: str, *, correlation_id: str,
                         context: str = "", metadata: dict[str, str] | None = None) -> AgentInvocation:
        invocation = self._workbench.invocation(
            name, prompt, correlation_id=correlation_id, context=context, metadata=metadata,
        )
        if invocation.registration.name not in self._registrations:
            raise AgentRegistryError("workbench mode is not admitted")
        return invocation

    def create(self, launch: AgentLaunch) -> Any:
        receipt = self._admit(launch)
        try:
            result = self._backend.create(launch)
        except Exception:
            self._release(receipt)
            raise
        if result is None:
            self._release(receipt)
            raise AgentRegistryError("backend refused agent admission")
        return result

    def resume(self, agent_id: str) -> Any:
        current = self._backend.status(agent_id)
        status = self._status(current)
        if status not in {"interrupted", "failed"}:
            raise AgentRegistryError("restart truth does not permit resume")
        return self._backend.resume(agent_id)

    def cancel(self, agent_id: str, *, reason: str = "") -> Any:
        result = self._backend.cancel(agent_id, reason=reason)
        self._release_by_agent(agent_id)
        return result

    def stop(self, agent_id: str, *, reason: str = "") -> Any:
        result = self._backend.stop(agent_id, reason=reason)
        self._release_by_agent(agent_id)
        return result

    def status(self, agent_id: str) -> Any:
        return self._backend.status(agent_id)

    def _admit(self, launch: AgentLaunch) -> AdmissionReceipt:
        budget = launch.budget
        if budget is None:
            raise AgentRegistryError("explicit budget is required for unified admission")
        lineage = launch.lineage or AgentLineage(launch.parent_id or launch.agent_id)
        if lineage.depth > budget.max_depth:
            raise AgentRegistryError("lineage depth exceeds budget")
        if launch.parent_id and launch.parent_id == launch.agent_id:
            raise AgentRegistryError("lineage cycle detected")
        active = self._active.get(lineage.root_id, 0)
        if active >= budget.max_concurrency:
            raise AgentRegistryError("concurrency budget exhausted")
        if launch.parent_id:
            children = self._children.get(launch.parent_id, 0)
            if children >= budget.max_children:
                raise AgentRegistryError("child budget exhausted")
        receipt = AdmissionReceipt(launch.agent_id, lineage.root_id, lineage.depth, budget)
        self._active[lineage.root_id] = active + 1
        if launch.parent_id:
            self._children[launch.parent_id] = self._children.get(launch.parent_id, 0) + 1
        return receipt

    def _release(self, receipt: AdmissionReceipt) -> None:
        self._active[receipt.root_id] = max(0, self._active.get(receipt.root_id, 1) - 1)
        if self._active[receipt.root_id] == 0:
            self._active.pop(receipt.root_id, None)

    def _release_by_agent(self, agent_id: str) -> None:
        # A cancelled/ stopped backend record is terminal; release the single
        # process-local reservation conservatively without fabricating state.
        for root_id, count in tuple(self._active.items()):
            if count:
                self._active[root_id] = count - 1
                if self._active[root_id] <= 0:
                    self._active.pop(root_id, None)
                break

    @staticmethod
    def _status(value: Any) -> str:
        if isinstance(value, dict):
            return str(value.get("status") or "")
        return str(getattr(value, "status", value) or "")

    # FleetAutopilotAdapter's registry protocol delegates here.
    def __getattr__(self, name: str) -> Any:
        if name in {"create", "resume", "cancel", "stop", "status"}:
            return object.__getattribute__(self, name)
        raise AttributeError(name)


__all__ = ["AdmissionReceipt", "UnifiedAgentRegistryBackend", "UnifiedAgentRegistryService"]
