"""MODEL-008: role-budget enforcement at the typed model route boundary."""

import pytest

from sonder_runtime.application.routing.capability_router import (
    CapabilityRouter,
    RoutingRequest,
)
from sonder_runtime.domain.agents.roles import AgentRole, BudgetLimit, role_budget
from sonder_runtime.domain.routing.capability_profiles import Capability, CapabilityProfile


PROFILES = (
    CapabilityProfile("editor", frozenset({Capability.EDIT}), quality=.8),
)


def test_caller_budget_must_fit_immutable_role_budget():
    role_limit = role_budget(AgentRole.EDITOR).limit
    decision = CapabilityRouter(PROFILES).route(
        RoutingRequest(AgentRole.EDITOR, requested_budget=BudgetLimit(
            steps=role_limit.steps,
            output_tokens=role_limit.output_tokens,
            wall_seconds=role_limit.wall_seconds,
        ))
    )
    assert decision.budget == role_limit


@pytest.mark.parametrize(
    "requested",
    [
        BudgetLimit(steps=21),
        BudgetLimit(output_tokens=6001),
        BudgetLimit(wall_seconds=601),
    ],
)
def test_over_budget_route_fails_closed_before_model_selection(requested):
    with pytest.raises(ValueError, match="exceeds immutable role budget"):
        CapabilityRouter(PROFILES).route(
            RoutingRequest(AgentRole.EDITOR, requested_budget=requested)
        )


def test_capability_rejection_is_preserved_alongside_budget_admission():
    with pytest.raises(ValueError, match="capabilities"):
        CapabilityRouter(PROFILES).route(
            RoutingRequest(
                AgentRole.EDITOR,
                required=frozenset({Capability.VISION}),
                requested_budget=BudgetLimit(steps=1),
            )
        )


def test_requested_budget_is_typed_and_immutable():
    with pytest.raises(ValueError, match="requested_budget"):
        RoutingRequest(AgentRole.EDITOR, requested_budget={"steps": 1})
