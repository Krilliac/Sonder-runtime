"""Durable attended route selection composed with training deployment.

The route planner remains pure.  This boundary is the small application seam
that records a selected route only after the existing attended, health-gated
activation succeeds, and records rollback as a durable route-history event.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from ...domain.common.errors import InvalidInput
from ...domain.routing.route_planner import AvailableModels, ModelRoute, RoutePlanner, RoutingRequest
from .deployment_rollback import DeploymentRollbackService, Route


@dataclass(frozen=True)
class DurableRouteSelection:
    selection_id: str
    operation: str
    artifact_id: str
    route_id: str
    lane: str
    tier: str
    model: str
    provider: str
    prior_route_id: str = ""
    reason: str = ""


class RouteSelectionStore(Protocol):
    def append(self, selection: DurableRouteSelection) -> None: ...
    def history(self) -> tuple[DurableRouteSelection, ...]: ...


class InMemoryRouteSelectionStore:
    """Reference append-only store; a durable adapter can implement the port."""

    def __init__(self) -> None:
        self._history: list[DurableRouteSelection] = []

    def append(self, selection: DurableRouteSelection) -> None:
        if any(item.selection_id == selection.selection_id for item in self._history):
            raise InvalidInput(f"duplicate route selection {selection.selection_id!r}")
        self._history.append(selection)

    def history(self) -> tuple[DurableRouteSelection, ...]:
        return tuple(self._history)


class AttendedRouteActivationBoundary:
    """Compose pure selection with attended activation and rollback."""

    def __init__(
        self,
        planner: RoutePlanner,
        deployment: DeploymentRollbackService,
        selections: RouteSelectionStore,
        *,
        policy: dict,
        available: AvailableModels,
    ) -> None:
        self._planner = planner
        self._deployment = deployment
        self._selections = selections
        self._policy = policy
        self._available = available

    def activate(
        self,
        selection_id: str,
        artifact_id: str,
        request: RoutingRequest,
        *,
        attended: bool = False,
    ) -> DurableRouteSelection:
        if not selection_id.strip():
            raise InvalidInput("selection_id is required")
        self._ensure_new_selection(selection_id)
        route = self._planner.select(request, self._policy, self._available)
        activated = self._deployment.activate(artifact_id, tier=route.tier, attended=attended)
        prior_route_id = self._deployment.history()[-1].prior_route_id
        selection = self._record(
            selection_id,
            operation="activate",
            artifact_id=artifact_id,
            activated=activated,
            route=route,
            prior_route_id=prior_route_id,
        )
        return selection

    def rollback(self, selection_id: str, *, attended: bool = False, reason: str = "operator rollback") -> DurableRouteSelection:
        if not selection_id.strip():
            raise InvalidInput("selection_id is required")
        self._ensure_new_selection(selection_id)
        prior = self._deployment.rollback(attended=attended, reason=reason)
        route_event = self._deployment.history()[-1]
        previous = next((item for item in reversed(self._selections.history()) if item.route_id == prior.route_id), None)
        if previous is None:
            raise InvalidInput("rolled-back route has no durable selection")
        selection = DurableRouteSelection(
            selection_id=selection_id,
            operation="rollback",
            artifact_id=prior.artifact_id,
            route_id=prior.route_id,
            lane=previous.lane,
            tier=previous.tier,
            model=previous.model,
            provider=previous.provider,
            prior_route_id=route_event.prior_route_id,
            reason=reason,
        )
        self._selections.append(selection)
        return selection

    def _ensure_new_selection(self, selection_id: str) -> None:
        if any(item.selection_id == selection_id for item in self._selections.history()):
            raise InvalidInput(f"duplicate route selection {selection_id!r}")

    def _record(
        self,
        selection_id: str,
        *,
        operation: str,
        artifact_id: str,
        activated: Route,
        route: ModelRoute,
        prior_route_id: str,
    ) -> DurableRouteSelection:
        selection = DurableRouteSelection(
            selection_id=selection_id,
            operation=operation,
            artifact_id=artifact_id,
            route_id=activated.route_id,
            lane=route.lane,
            tier=route.tier,
            model=route.model,
            provider=route.provider,
            prior_route_id=prior_route_id,
        )
        self._selections.append(selection)
        return selection


__all__ = [
    "AttendedRouteActivationBoundary",
    "DurableRouteSelection",
    "InMemoryRouteSelectionStore",
    "RouteSelectionStore",
]
