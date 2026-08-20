import pytest

from sonder_runtime.application.loop_event_classification import (
    DurableSessionFact,
    EphemeralLiveEvent,
    EventRetention,
    LoopEventClass,
    UnknownLoopEventError,
    classify_event,
    is_durable_session_fact,
    is_ephemeral_live_event,
)


def test_durable_session_facts_are_distinct_from_live_events():
    durable = classify_event("message.received")
    interception = classify_event("pre_step")
    capability = classify_event("capability.selected")

    assert durable.event_class is LoopEventClass.DURABLE_SESSION_FACT
    assert durable.retention is EventRetention.DURABLE
    assert interception.event_class is LoopEventClass.EPHEMERAL_INTERCEPTION
    assert capability.event_class is LoopEventClass.EPHEMERAL_CAPABILITY
    assert interception.is_ephemeral and capability.is_ephemeral


def test_classification_is_explicit_and_fails_closed_for_unknown_types():
    assert is_durable_session_fact("tool.completed")
    assert is_ephemeral_live_event("retry")
    with pytest.raises(UnknownLoopEventError):
        classify_event("future.event")
    with pytest.raises(UnknownLoopEventError):
        classify_event(" ")


def test_envelopes_enforce_their_side_of_the_boundary_and_copy_payloads():
    payload = {"message_id": "m1"}
    fact = DurableSessionFact("message.completed", "session-1", payload)
    live = EphemeralLiveEvent("capability.check", {"name": "vision"})
    payload["changed"] = True

    assert dict(fact.payload) == {"message_id": "m1"}
    assert dict(live.payload) == {"name": "vision"}
    with pytest.raises(ValueError):
        DurableSessionFact("pre_step", "session-1")
    with pytest.raises(ValueError):
        EphemeralLiveEvent("session.started")


def test_envelopes_are_immutable_and_reject_empty_session_identity():
    fact = DurableSessionFact("session.started", "session-1")
    with pytest.raises(TypeError):
        fact.payload["x"] = 1
    with pytest.raises(ValueError):
        DurableSessionFact("session.started", " ")
