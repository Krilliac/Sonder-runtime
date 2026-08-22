"""Immutable, attended model activation with health-gated rollback.

This slice deliberately stays behind the existing ``DeploymentService``.  It
models the safety boundary needed by training: artifacts are content-addressed,
activation is attended, a failed health gate leaves the prior route active,
and rollback is an explicit operation recorded in history.
"""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from typing import Callable, Protocol

from ...domain.common.errors import Conflict, Forbidden, InvalidInput, NotFound


@dataclass(frozen=True)
class ImmutableArtifact:
    artifact_id: str
    model_id: str
    revision: str
    content_digest: str

    @classmethod
    def create(cls, artifact_id: str, model_id: str, revision: str, content: bytes) -> "ImmutableArtifact":
        if not artifact_id or not model_id or not revision or not content:
            raise InvalidInput("artifact identity and content are required")
        return cls(artifact_id, model_id, revision, sha256(content).hexdigest())


@dataclass(frozen=True)
class HealthReport:
    healthy: bool
    checks: tuple[str, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class Route:
    route_id: str
    artifact_id: str
    tier: str
    generation: int


@dataclass(frozen=True)
class DeploymentEvent:
    operation: str
    route_id: str
    artifact_id: str
    prior_route_id: str = ""
    reason: str = ""


class HealthGate(Protocol):
    def __call__(self, artifact: ImmutableArtifact) -> HealthReport: ...


class DeploymentRepository(Protocol):
    def artifact(self, artifact_id: str) -> ImmutableArtifact | None: ...
    def active(self) -> Route | None: ...
    def activate(self, route: Route) -> None: ...
    def history(self) -> tuple[DeploymentEvent, ...]: ...
    def record(self, event: DeploymentEvent) -> None: ...


class InMemoryDeploymentRepository:
    """Small reference repository; production adapters can preserve the same port."""

    def __init__(self) -> None:
        self._artifacts: dict[str, ImmutableArtifact] = {}
        self._active: Route | None = None
        self._events: list[DeploymentEvent] = []

    def add_artifact(self, artifact: ImmutableArtifact) -> None:
        existing = self._artifacts.get(artifact.artifact_id)
        if existing is not None and existing != artifact:
            raise Conflict("artifact identities are immutable")
        self._artifacts[artifact.artifact_id] = artifact

    def artifact(self, artifact_id: str) -> ImmutableArtifact | None:
        return self._artifacts.get(artifact_id)

    def active(self) -> Route | None:
        return self._active

    def activate(self, route: Route) -> None:
        self._active = route

    def history(self) -> tuple[DeploymentEvent, ...]:
        return tuple(self._events)

    def record(self, event: DeploymentEvent) -> None:
        self._events.append(event)


class DeploymentRollbackService:
    def __init__(self, repository: DeploymentRepository, health_gate: HealthGate) -> None:
        self._repository = repository
        self._health_gate = health_gate

    def activate(self, artifact_id: str, *, tier: str = "code", attended: bool = False) -> Route:
        if not attended:
            raise Forbidden("deployment activation requires an attended operator")
        artifact = self._repository.artifact(artifact_id)
        if artifact is None:
            raise NotFound(f"artifact {artifact_id!r} not found")
        report = self._health_gate(artifact)
        if not report.healthy:
            raise Conflict(f"health gate rejected activation: {report.reason or 'unhealthy'}")
        prior = self._repository.active()
        route = Route(artifact_id, artifact.artifact_id, tier, (prior.generation + 1) if prior else 1)
        self._repository.activate(route)
        self._repository.record(DeploymentEvent("activate", route.route_id, artifact.artifact_id, prior.route_id if prior else ""))
        return route

    def rollback(self, *, attended: bool = False, reason: str = "operator rollback") -> Route:
        if not attended:
            raise Forbidden("rollback requires an attended operator")
        current = self._repository.active()
        if current is None:
            raise InvalidInput("no active route to roll back")
        events = self._repository.history()
        prior_id = next((event.prior_route_id for event in reversed(events) if event.route_id == current.route_id and event.prior_route_id), "")
        if not prior_id:
            raise InvalidInput("no retained prior route")
        target_event = next((event for event in events if event.route_id == prior_id), None)
        if target_event is None:
            raise NotFound("retained prior route is missing from history")
        target = Route(prior_id, target_event.artifact_id, current.tier, current.generation + 1)
        self._repository.activate(target)
        self._repository.record(DeploymentEvent("rollback", target.route_id, target.artifact_id, current.route_id, reason))
        return target

    def history(self) -> tuple[DeploymentEvent, ...]:
        return self._repository.history()
