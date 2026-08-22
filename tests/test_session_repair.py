from __future__ import annotations

from sonder_runtime.application.session.repair import diagnose_session_tail, plan_session_resume
from sonder_runtime.domain.common.events import DomainEvent


def event(sequence, event_type, payload=None, *, session="session_1234567890abcdef1234567890abcdef"):
    return DomainEvent(event_type, "session", session, sequence, payload or {})


def test_clean_tail_resumes_after_last_durable_event():
    events = [event(1, "session.started"), event(2, "message.received", {"message_id": "m", "role": "user"})]
    plan = plan_session_resume(events)
    assert plan.diagnosis.disposition == "clean"
    assert plan.valid_boundary == 2
    assert plan.resume_sequence == 3
    assert plan.safe_prefix == tuple(events)
    assert plan.discarded_tail == ()


def test_inflight_tail_stops_before_effect_and_does_not_replay_it():
    events = [event(1, "session.started"), event(2, "model.requested", {"request_id": "r", "model": "local"}), event(3, "model.started", {"request_id": "r", "model": "local"})]
    plan = plan_session_resume(events)
    assert plan.diagnosis.disposition == "truncated"
    assert plan.valid_boundary == 1
    assert plan.resume_sequence == 2
    assert plan.safe_prefix == (events[0],)
    assert plan.discarded_tail == tuple(events[1:])
    assert plan.diagnosis.issues[0].code == "truncated_tail"


def test_legacy_model_response_closes_a_request_without_repairing_the_tail():
    events = [
        event(1, "session.started"),
        event(2, "model.requested", {"turn_id": "t1", "prompt": "hi"}),
        event(3, "model.response", {"turn_id": "t1", "content": "hello"}),
    ]
    diagnosis = diagnose_session_tail(events)
    assert diagnosis.disposition == "clean"
    assert diagnosis.valid_boundary == 3


def test_gap_and_cross_session_tail_are_inconsistent_and_not_resumable():
    gap = diagnose_session_tail([event(1, "session.started"), event(3, "session.paused")])
    assert gap.disposition == "inconsistent"
    assert not gap.can_resume
    assert gap.valid_boundary == 1
    foreign = diagnose_session_tail([event(1, "session.started"), event(2, "session.paused", session="session_fedcba9876543210fedcba9876543210")])
    assert foreign.disposition == "inconsistent"
    assert foreign.valid_boundary == 1


def test_diagnosis_is_order_independent_and_does_not_mutate_input():
    events = [event(2, "session.paused"), event(1, "session.started")]
    original = tuple(events)
    plan = plan_session_resume(events)
    assert tuple(events) == original
    assert plan.safe_prefix == tuple(sorted(events, key=lambda item: item.sequence))
