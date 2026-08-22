"""Focused proof for the composed API-001/002/004/005/006/007/008 seam."""
from __future__ import annotations

import pytest

from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.protocol import (
    ProtocolApplicationFacade,
    ProtocolAuthorizationError,
    ProtocolEventType,
    ReconnectRequest,
    ResumeCursor,
    ResumableStream,
)
from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs


class Authorizer:
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.calls = []

    def authorize(self, operation, client_id):
        self.calls.append((operation, client_id))
        return self.allowed


def facade(authorizer, events):
    catalogs = GeneratedCatalogs.generate(
        InMemoryToolRegistry((ToolDescriptor("status"),)), commands=("status",)
    )
    stream = ResumableStream("session-1", capacity=2)
    return ProtocolApplicationFacade.compose(
        catalogs, streams={"session-1": stream}, authorization=authorizer,
        event_hook=events.append,
    ), stream


def test_composed_facade_authorizes_reconnect_and_emits_provider_neutral_events():
    events = []
    auth = Authorizer(True)
    protocol, stream = facade(auth, events)
    protocol.publish("session-1", ProtocolEventType.SESSION_UPDATED, {"state": "ready"}, event_id="e1")
    response = protocol.reconnect(
        ReconnectRequest("mobile-1", protocol.schema.digest, (ResumeCursor("session-1", 0),))
    )
    assert response.resumed
    assert auth.calls == [("protocol.reconnect", "mobile-1")]
    assert [event["kind"] for event in events] == [
        ProtocolEventType.SESSION_UPDATED.value,
        ProtocolEventType.CONNECTION_RECONNECTED.value,
    ]
    assert stream.watermark == 1


def test_composed_facade_fails_closed_without_authorization():
    protocol, _ = facade(Authorizer(False), [])
    with pytest.raises(ProtocolAuthorizationError):
        protocol.reconnect(
            ReconnectRequest("mobile-1", protocol.schema.digest, (ResumeCursor("session-1", 0),))
        )


def test_composed_facade_rejects_identity_confusion_before_authorizer():
    auth = Authorizer(True)
    protocol, _ = facade(auth, [])
    with pytest.raises(ProtocolAuthorizationError):
        protocol.reconnect(
            ReconnectRequest("mobile-1", protocol.schema.digest, (ResumeCursor("session-1", 0),)),
            client_id="mobile-2",
        )
    assert auth.calls == []


def test_authorized_host_can_open_one_bounded_stream_and_reconnect_to_it():
    events = []
    auth = Authorizer(True)
    protocol, stream = facade(auth, events)
    opened = protocol.open_stream("session-2", client_id="mobile-2", capacity=4)
    assert opened.stream_id == "session-2"
    protocol.publish("session-2", ProtocolEventType.SESSION_UPDATED,
                     {"state": "ready"}, event_id="e2")
    response = protocol.reconnect(
        ReconnectRequest("mobile-2", protocol.schema.digest,
                         (ResumeCursor("session-2", 0),))
    )
    assert response.resumed
    assert response.results[0].batch.events[0].event_id == "e2"
    assert events[0]["kind"] == "stream.opened"
    assert opened.watermark == 1


def test_default_protocol_policy_rejects_stream_creation():
    catalogs = GeneratedCatalogs.generate(
        InMemoryToolRegistry((ToolDescriptor("status"),)), commands=("status",)
    )
    protocol = ProtocolApplicationFacade.compose(catalogs)
    with pytest.raises(ProtocolAuthorizationError):
        protocol.open_stream("session-2", client_id="mobile-2")


def test_authorized_host_can_close_stream_and_future_resume_is_rejected():
    events = []
    auth = Authorizer(True)
    protocol, _ = facade(auth, events)
    protocol.open_stream("session-2", client_id="mobile-2")
    protocol.close_stream("session-2", client_id="mobile-2", reason="finished")

    response = protocol.reconnect(
        ReconnectRequest("mobile-2", protocol.schema.digest,
                         (ResumeCursor("session-2", 0),))
    )
    assert not response.resumed
    assert response.results[0].reason == "unknown stream"
    assert events[-2] == {
        "kind": "stream.closed", "stream_id": "session-2",
        "client_id": "mobile-2", "reason": "finished",
    }
