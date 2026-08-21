"""TRAIN-006: attended deployment and durable active-route composition."""
from __future__ import annotations

import pytest

from sonder_runtime.application.training.deployment_rollback import (
    DeploymentRollbackService,
    HealthReport,
    ImmutableArtifact,
    InMemoryDeploymentRepository,
)
from sonder_runtime.application.training.route_activation import (
    AttendedRouteActivationBoundary,
    InMemoryRouteSelectionStore,
)
from sonder_runtime.domain.agents.roles import AgentRole
from sonder_runtime.domain.routing.capability_profiles import CapabilityProfile, Capability
from sonder_runtime.domain.routing.route_planner import AvailableModels, RoutePlanner, RoutingRequest
from sonder_runtime.domain.runtime_policy.rules import default_policy
from sonder_runtime.domain.common.errors import Conflict, Forbidden, InvalidInput


def _boundary(healthy=True):
    deployments = InMemoryDeploymentRepository()
    deployments.add_artifact(ImmutableArtifact.create("a1", "model", "r1", b"one"))
    deployments.add_artifact(ImmutableArtifact.create("a2", "model", "r2", b"two"))
    health = lambda _: HealthReport(healthy, reason="smoke failed" if not healthy else "")
    available = AvailableModels({"code": "sonder:latest", "fast": "sonder:latest"})
    selections = InMemoryRouteSelectionStore()
    return (
        deployments,
        selections,
        AttendedRouteActivationBoundary(
            RoutePlanner(), DeploymentRollbackService(deployments, health),
            selections, policy=default_policy(env={}), available=available,
        ),
    )


def _request():
    return RoutingRequest(lane="workbench", prompt="implement a function")


def test_attended_activation_persists_selected_route_after_health_gate():
    repo, _, boundary = _boundary()
    selection = boundary.activate("s1", "a1", _request(), attended=True)
    assert selection.operation == "activate"
    assert selection.tier == "code"
    assert repo.active().route_id == selection.route_id


def test_activation_requires_attendance_and_does_not_persist_on_refusal():
    _, selections, boundary = _boundary()
    with pytest.raises(Forbidden):
        boundary.activate("s1", "a1", _request())
    assert selections.history() == ()


def test_unhealthy_activation_leaves_active_route_and_selection_history_unchanged():
    repo, selections, boundary = _boundary(healthy=False)
    with pytest.raises(Conflict):
        boundary.activate("s1", "a1", _request(), attended=True)
    assert repo.active() is None
    assert selections.history() == ()


def test_rollback_restores_prior_durable_route_and_records_reason():
    repo, _, boundary = _boundary()
    first = boundary.activate("s1", "a1", _request(), attended=True)
    second = boundary.activate("s2", "a2", _request(), attended=True)
    restored = boundary.rollback("s3", attended=True, reason="regression")
    assert restored.operation == "rollback"
    assert restored.route_id == first.route_id
    assert restored.prior_route_id == second.route_id
    assert restored.reason == "regression"
    assert repo.active().route_id == first.route_id


def test_duplicate_selection_is_rejected_before_active_route_changes():
    repo, selections, boundary = _boundary()
    boundary.activate("s1", "a1", _request(), attended=True)
    with pytest.raises(InvalidInput, match="duplicate route selection"):
        boundary.activate("s1", "a2", _request(), attended=True)
    assert repo.active().artifact_id == "a1"
    assert len(selections.history()) == 1
