"""Focused MODEL-007 contract tests."""

import pytest

from sonder_runtime.application.model_gateway.escalation import (
    ControlledEscalationPolicy,
    ControlledEscalationService,
    EscalationReason,
    EscalationRequest,
    EscalationRoute,
    MAX_ESCALATIONS,
    MAX_ROUTES,
)


ROUTES = (
    EscalationRoute("fast", "local-fast", 0),
    EscalationRoute("reasoning", "local-reasoning", 1),
    EscalationRoute("review", "local-review", 2),
)


class Events:
    def __init__(self):
        self.items = []

    def emit(self, event_code, **kwargs):
        self.items.append((event_code, kwargs))


def test_uncertainty_escalates_once_and_carries_typed_reason_and_provenance():
    service = ControlledEscalationService(ControlledEscalationPolicy(max_escalations=1))
    request = EscalationRequest(
        "req-uncertain", ROUTES, "fast", uncertainty=0.9,
        provenance=("classifier:run-17", "calibration:model-a"),
    )

    decision = service.decide(request)

    assert decision.escalated is True
    assert decision.selected_route.route_id == "reasoning"
    assert decision.reason is EscalationReason.UNCERTAINTY
    assert decision.provenance == request.provenance
    assert decision.can_escalate is False


def test_verifier_failure_takes_precedence_and_outcome_records_helpfulness():
    request = EscalationRequest(
        "req-verifier", ROUTES, "fast", uncertainty=0.1, verifier_passed=False,
        provenance=("verifier:sha256:abc",),
    )
    service = ControlledEscalationService()
    decision = service.decide(request)
    outcome = service.record_outcome(request, decision, helped=False, evidence="review remained failed")

    assert decision.reason is EscalationReason.VERIFIER_FAILURE
    assert outcome.helped is False
    assert outcome.provenance == ("verifier:sha256:abc",)


def test_missing_trigger_provenance_fails_closed_without_escalating():
    request = EscalationRequest("req-no-proof", ROUTES, "fast", uncertainty=0.99)
    decision = ControlledEscalationService().decide(request)

    assert decision.escalated is False
    assert decision.denied is True
    assert decision.trigger == "missing_provenance"
    assert decision.selected_route.route_id == "fast"


@pytest.mark.parametrize("field", ["request_budget", "policy_budget", "route_count"])
def test_absolute_limits_reject_unbounded_requests(field):
    if field == "request_budget":
        with pytest.raises(ValueError):
            EscalationRequest("r", ROUTES, "fast", max_escalations=MAX_ESCALATIONS + 1)
    elif field == "policy_budget":
        with pytest.raises(ValueError):
            ControlledEscalationPolicy(max_escalations=MAX_ESCALATIONS + 1)
    else:
        routes = tuple(EscalationRoute(f"r{i}", f"m{i}", i) for i in range(MAX_ROUTES + 1))
        with pytest.raises(ValueError):
            EscalationRequest("r", routes, "r0")


def test_routes_must_have_strictly_ordered_unique_ranks_and_service_emits_bounded_decision():
    with pytest.raises(ValueError):
        EscalationRequest(
            "r", (EscalationRoute("a", "a", 0), EscalationRoute("b", "b", 0)), "a"
        )
    events = Events()
    request = EscalationRequest("r-event", ROUTES, "fast", verifier_passed=False, provenance=("v:1",))
    decision = ControlledEscalationService(event_sink=events).decide(request)
    assert events.items[0][0] == "model.escalation.decided"
    assert events.items[0][1]["detail"]["to_route"] == decision.selected_route.route_id


def test_outcome_requires_provenance_and_cannot_be_fabricated_for_denied_decision():
    service = ControlledEscalationService()
    request = EscalationRequest("r-denied", ROUTES, "fast", uncertainty=0.9)
    decision = service.decide(request)
    with pytest.raises(ValueError):
        service.record_outcome(request, decision, helped=True, evidence="not applicable")
