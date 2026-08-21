from __future__ import annotations

import io
import json
from uuid import uuid4

import pytest

from sonder_runtime.application.protocol.editor_interop import ImplementationInfo, ProtocolEnvelope
from sonder_runtime.interfaces.editor.transport import (
    EditorStdioTransport,
    EditorTransportError,
    EditorTransportLimits,
)


def _frame(message_type, payload):
    return json.dumps(ProtocolEnvelope.create(message_type, payload).to_dict()) + "\n"


def test_editor_stdio_transport_initializes_correlates_and_dispatches():
    incoming = io.StringIO(
        _frame("session/initialize", {"implementation": {"name": "client", "version": "1"}})
        + _frame("rules/import", {"paths": ["AGENTS.md"]})
    )
    outgoing = io.StringIO()
    transport = EditorStdioTransport(
        incoming,
        outgoing,
        server=ImplementationInfo("sonder", "1", frozenset({"cancel"})),
        handler=lambda envelope: {"accepted": envelope.payload["paths"]},
    )

    assert transport.serve() == 2
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert responses[0]["message_type"] == "session/initialized"
    assert responses[0]["payload"]["request_id"]
    assert responses[1]["message_type"] == "rules/import/result"
    assert responses[1]["payload"]["accepted"] == ["AGENTS.md"]


def test_editor_stdio_transport_requires_initialization_and_rejects_oversize():
    outgoing = io.StringIO()
    transport = EditorStdioTransport(
        io.StringIO(_frame("rules/import", {"paths": []}) + "x" * 600),
        outgoing,
        server=ImplementationInfo("sonder", "1"),
        limits=EditorTransportLimits(max_frame_bytes=512, max_messages=2),
    )
    assert transport.serve() == 2
    responses = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert all(response["message_type"] == "error" for response in responses)

    with pytest.raises(ValueError):
        EditorTransportLimits(max_frame_bytes=0)


def test_editor_stdio_transport_validates_and_delivers_cancellation():
    cancelled = []
    request_id = str(uuid4())
    incoming = io.StringIO(
        _frame("session/initialize", {})
        + _frame(
            "session/cancel_request",
            {"request_id": request_id, "session_id": "s-1", "reason": "user"},
        )
    )
    outgoing = io.StringIO()
    transport = EditorStdioTransport(
        incoming,
        outgoing,
        server=ImplementationInfo("sonder", "1"),
        cancellation_handler=cancelled.append,
    )
    assert transport.serve() == 2
    assert cancelled[0].request_id == request_id
    assert json.loads(outgoing.getvalue().splitlines()[1])["message_type"] == "session/cancelled"


def test_editor_stdio_transport_does_not_expose_handler_exception_details():
    transport = EditorStdioTransport(
        io.StringIO(_frame("session/initialize", {}) + _frame("rules/import", {})),
        io.StringIO(),
        server=ImplementationInfo("sonder", "1"),
        handler=lambda _: (_ for _ in ()).throw(RuntimeError("secret detail")),
    )
    transport.serve()
    assert "secret detail" not in transport._output.getvalue()
