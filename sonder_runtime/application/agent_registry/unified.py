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
    parent_id: str = ""


_TERMINAL = frozenset({"cancelled", "stopped", "failed", "completed"})


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
        self._receipts: dict[str, AdmissionReceipt] = {}

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
        # An agent that already holds a reservation is an idempotent re-create:
        # never take a second reservation, and never settle the original one
        # on a failed retry.
        fresh = launch.agent_id not in self._receipts
        if fresh:
            self._admit(launch)
        try:
            result = self._backend.create(launch)
        except Exception:
            if fresh:
                self.settle(launch.agent_id)
            raise
        if result is None:
            if fresh:
                self.settle(launch.agent_id)
            raise AgentRegistryError("backend refused agent admission")
        if fresh:
            # The backend may accept the create yet return an already-terminal
            # row (e.g. a child of a cancelled parent).  Nothing later can
            # cancel such a row, so release its reservation now.
            self._settle_if_terminal(launch.agent_id, result)
        return result

    def resume(self, agent_id: str) -> Any:
        current = self._backend.status(agent_id)
        status = self._status(current)
        if status not in {"interrupted", "failed"}:
            raise AgentRegistryError("restart truth does not permit resume")
        return self._backend.resume(agent_id)

    def cancel(self, agent_id: str, *, reason: str = "") -> Any:
        result = self._backend.cancel(agent_id, reason=reason)
        self._settle_if_terminal(agent_id, result)
        return result

    def stop(self, agent_id: str, *, reason: str = "") -> Any:
        result = self._backend.stop(agent_id, reason=reason)
        self._settle_if_terminal(agent_id, result)
        return result

    def status(self, agent_id: str) -> Any:
        return self._backend.status(agent_id)

    def _admit(self, launch: AgentLaunch) -> AdmissionReceipt:
        budget = launch.budget
        if budget is None:
            raise AgentRegistryError("explicit budget is required for unified admission")
        if launch.agent_id in self._receipts:
            raise AgentRegistryError("agent already admitted")
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
        receipt = AdmissionReceipt(
            launch.agent_id, lineage.root_id, lineage.depth, budget, launch.parent_id or "",
        )
        self._receipts[launch.agent_id] = receipt
        self._active[lineage.root_id] = active + 1
        if launch.parent_id:
            self._children[launch.parent_id] = self._children.get(launch.parent_id, 0) + 1
        return receipt

    def settle(self, agent_id: str) -> None:
        """Release the reservation held by ``agent_id``, exactly once.

        Unknown or already-settled agents change nothing.
        """
        receipt = self._receipts.pop(agent_id, None)
        if receipt is None:
            return
        remaining = self._active.get(receipt.root_id, 0) - 1
        if remaining > 0:
            self._active[receipt.root_id] = remaining
        else:
            self._active.pop(receipt.root_id, None)
        if receipt.parent_id:
            left = self._children.get(receipt.parent_id, 0) - 1
            if left > 0:
                self._children[receipt.parent_id] = left
            else:
                self._children.pop(receipt.parent_id, None)

    def _settle_if_terminal(self, agent_id: str, result: Any) -> None:
        # A backend that only acknowledges the request (row still running or
        # cancel_requested) keeps the reservation.
        # A bulk result (fleet store cancel_agents) carries per-agent rows:
        # settle every row the backend reports terminal, including cascaded
        # children and prefix matches, and nothing else.
        agents = result.get("agents") if isinstance(result, dict) else None
        if isinstance(agents, (list, tuple)):
            for row in agents:
                if not isinstance(row, dict):
                    continue
                row_id = str(row.get("id") or "")
                if row_id and self._status(row) in _TERMINAL:
                    self.settle(row_id)
            return
        if self._status(result) in _TERMINAL:
            self.settle(agent_id)

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


__all__ = ["AdmissionReceipt", "UnifiedAgentRegistryService"]
