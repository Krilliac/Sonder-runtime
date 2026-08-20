from sonder_runtime.application.routing.capability_router import (
    CapabilityRouter,
    RoutingRequest,
)
from sonder_runtime.domain.agents.roles import AgentRole
from sonder_runtime.domain.routing.capability_profiles import Capability, CapabilityProfile


PROFILES = (
    CapabilityProfile("small-editor", frozenset({Capability.EDIT}), quality=.6, escalation_rank=0),
    CapabilityProfile("strong-editor", frozenset({Capability.EDIT, Capability.VERIFY, Capability.STRUCTURED}), quality=.9, escalation_rank=1),
)


def test_role_requirements_select_capable_profile_and_own_budget():
    decision = CapabilityRouter(PROFILES).route(RoutingRequest(AgentRole.EDITOR))
    assert decision.model == "small-editor"
    assert decision.budget.output_tokens == 6000
    assert decision.escalated is False


def test_uncertainty_escalates_once_and_records_reason():
    decision = CapabilityRouter(PROFILES).route(
        RoutingRequest(AgentRole.EDITOR, uncertainty=.8)
    )
    assert (decision.model, decision.escalated, decision.reason) == (
        "strong-editor", True, "high_uncertainty"
    )


def test_verifier_failure_triggers_escalation():
    decision = CapabilityRouter(PROFILES).route(
        RoutingRequest(AgentRole.EDITOR, verifier_passed=False)
    )
    assert decision.reason == "verifier_failure"
    assert decision.model == "strong-editor"


def test_escalation_is_bounded_and_does_not_repeat_forever():
    router = CapabilityRouter(PROFILES, max_escalations=1)
    decision = router.route(RoutingRequest(AgentRole.EDITOR, uncertainty=.9, escalation_count=1))
    assert decision.escalated is False
    assert decision.reason == "escalation_limit"
    assert decision.can_escalate is False


def test_missing_capability_is_rejected_before_dispatch():
    try:
        CapabilityRouter(PROFILES).route(
            RoutingRequest(AgentRole.VERIFIER, required=frozenset({Capability.VISION}))
        )
    except ValueError as exc:
        assert "capabilities" in str(exc)
    else:
        raise AssertionError("expected capability rejection")


def test_role_budgets_are_separate():
    router = CapabilityRouter(
        (CapabilityProfile("planner", frozenset({Capability.PLAN})),
         CapabilityProfile("editor", frozenset({Capability.EDIT})))
    )
    architect = router.route(RoutingRequest(AgentRole.ARCHITECT))
    editor = router.route(RoutingRequest(AgentRole.EDITOR))
    assert architect.budget != editor.budget
