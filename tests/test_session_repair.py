from __future__ import annotations

from types import MappingProxyType

import pytest

from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import CapturedTool, SessionCaptureService
from sonder_runtime.application.session.continuity import SessionContinuityService
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


@pytest.mark.parametrize("payload_type", [dict, MappingProxyType])
def test_explicit_unknown_terminal_does_not_close_another_request(payload_type):
    diagnosis = diagnose_session_tail([
        event(1, "model.requested", payload_type({"request_id": "r1", "turn_id": "t1"})),
        event(2, "model.response", payload_type({
            "request_id": "r-other", "turn_id": "t1", "content": "answer",
        })),
    ])
    assert diagnosis.disposition == "inconsistent"
    assert not diagnosis.can_resume
    assert [(issue.sequence, issue.code) for issue in diagnosis.issues] == [
        (2, "unmatched_terminal"),
    ]


@pytest.mark.parametrize("payload_type", [dict, MappingProxyType])
def test_interleaved_response_leaves_the_other_request_pending(payload_type):
    events = [
        event(1, "session.started"),
        event(2, "model.requested", payload_type({"request_id": "r1", "turn_id": "t1"})),
        event(3, "model.requested", payload_type({"request_id": "r2", "turn_id": "t2"})),
        event(4, "model.response", payload_type({"request_id": "r2", "turn_id": "t2"})),
    ]
    plan = plan_session_resume(events)
    assert plan.diagnosis.disposition == "truncated"
    assert plan.valid_boundary == 1
    assert plan.safe_prefix == (events[0],)
    assert plan.discarded_tail == tuple(events[1:])


@pytest.mark.parametrize("payload_type", [dict, MappingProxyType])
def test_failed_and_successful_interleaved_requests_leave_a_clean_tail(payload_type):
    diagnosis = diagnose_session_tail([
        event(1, "model.requested", payload_type({"request_id": "r1", "turn_id": "t1"})),
        event(2, "model.requested", payload_type({"request_id": "r2", "turn_id": "t2"})),
        event(3, "model.failed", payload_type({
            "request_id": "r1", "turn_id": "t1", "error_code": "CANCELLED",
        })),
        event(4, "model.response", payload_type({"request_id": "r2", "turn_id": "t2"})),
    ])
    assert diagnosis.disposition == "clean"
    assert diagnosis.valid_boundary == 4


@pytest.mark.parametrize("terminal_payload", [{}, {"turn_id": "t1"}])
def test_ambiguous_legacy_terminal_does_not_choose_a_pending_request(terminal_payload):
    diagnosis = diagnose_session_tail([
        event(1, "model.requested", {"request_id": "r1", "turn_id": "t1"}),
        event(2, "model.requested", {"request_id": "r2", "turn_id": "t1"}),
        event(3, "model.response", terminal_payload),
    ])
    assert diagnosis.disposition == "inconsistent"
    assert not diagnosis.can_resume
    assert [(issue.sequence, issue.code) for issue in diagnosis.issues] == [
        (3, "ambiguous_terminal"),
    ]


@pytest.mark.parametrize("payload_type", [dict, MappingProxyType])
def test_legacy_terminal_selects_the_unique_matching_turn(payload_type):
    diagnosis = diagnose_session_tail([
        event(1, "model.requested", payload_type({"request_id": "r1", "turn_id": "t1"})),
        event(2, "model.requested", payload_type({"request_id": "r2", "turn_id": "t2"})),
        event(3, "model.response", payload_type({"turn_id": "t2"})),
    ])
    assert diagnosis.disposition == "truncated"
    assert diagnosis.valid_boundary == 0


def test_legacy_terminal_with_different_turn_does_not_close_request():
    diagnosis = diagnose_session_tail([
        event(1, "model.requested", {"request_id": "r1", "turn_id": "t1"}),
        event(2, "model.response", {"turn_id": "t-other"}),
    ])
    assert diagnosis.disposition == "truncated"
    assert diagnosis.valid_boundary == 0


def test_identityless_legacy_terminal_closes_only_pending_operation_in_family():
    diagnosis = diagnose_session_tail([
        event(1, "model.requested", {"request_id": "r1"}),
        event(2, "tool.requested", {"call_id": "c1"}),
        event(3, "model.response"),
    ])
    assert diagnosis.disposition == "truncated"
    assert diagnosis.valid_boundary == 1


def test_explicit_terminal_identity_cannot_match_another_family():
    diagnosis = diagnose_session_tail([
        event(1, "tool.requested", {"call_id": "r1"}),
        event(2, "model.response", {"request_id": "r1"}),
    ])
    assert diagnosis.disposition == "inconsistent"
    assert diagnosis.issues[0].code == "unmatched_terminal"


def test_retrospectively_captured_tool_turn_can_resume_after_repository_restart(tmp_path):
    database = tmp_path / "sessions.db"
    repository = SQLiteSessionRepository(database, max_read_limit=100)
    capture = SessionCaptureService(repository, replay_limit=100)
    session_id = "session_1234567890abcdef1234567890abcdef"
    capture.capture_turn(
        session_id, "t1", ModelRequest(prompt="inspect files", tier="local"),
        request_id="r1", user_message="inspect files", model_response="inspected",
        tools=(CapturedTool("c1", "list_files", {"path": "."}, ["README.md"]),),
    )
    reopened = SQLiteSessionRepository(database, max_read_limit=100)
    before = reopened.read_range(session_id, limit=100)
    plan = SessionContinuityService(reopened, max_events=100).resume(session_id)
    assert plan.diagnosis.disposition == "clean"
    assert plan.diagnosis.can_resume
    assert plan.valid_boundary == 5
    assert plan.resume_sequence == 6
    assert reopened.read_range(session_id, limit=100) == before


def test_legacy_tool_result_with_unknown_call_id_does_not_close_pending_call():
    diagnosis = diagnose_session_tail([
        event(1, "tool.call", {"call_id": "c1", "turn_id": "t1"}),
        event(2, "tool.result", {"call_id": "c-other", "turn_id": "t1"}),
        event(3, "tool.result", {"call_id": "c1", "turn_id": "t1"}),
    ])
    assert diagnosis.disposition == "inconsistent"
    assert [(issue.sequence, issue.code) for issue in diagnosis.issues] == [
        (2, "unmatched_terminal"),
    ]
