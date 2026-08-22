"""Provider-neutral reconnect wire contract tests."""
from __future__ import annotations

import pytest

from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.protocol.client_schema import (
    ClientParityContract, ReconnectRequest, ResumeCursor, build_client_schema,
)
from sonder_runtime.application.protocol.mobile_parity import (
    MobileWireError, decode_reconnect_request, encode_client_schema,
    encode_reconnect_request, encode_reconnect_response,
)
from sonder_runtime.application.protocol.resumable_streams import ResumableStream
from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs


def _schema():
    return build_client_schema(GeneratedCatalogs.generate(
        InMemoryToolRegistry((ToolDescriptor("status"),)), commands=("help",)
    ))


def test_mobile_request_round_trip_is_strict_and_provider_neutral():
    schema = _schema()
    request = ReconnectRequest("flutter-1", schema.digest, (ResumeCursor("s", 3),), 8)
    wire = encode_reconnect_request(request)
    assert decode_reconnect_request(wire) == request
    assert wire["type"] == "reconnect" and wire["version"] == 1
    with pytest.raises(MobileWireError):
        decode_reconnect_request({**wire, "provider": "ollama"})


def test_mobile_schema_and_response_envelopes_are_json_safe():
    schema = _schema()
    stream = ResumableStream("s")
    stream.publish("message", {"text": "hi"}, event_id="e1")
    response = ClientParityContract(schema, {"s": stream}).reconnect(
        ReconnectRequest("flutter-1", schema.digest, (ResumeCursor("s", 0),))
    )
    encoded = encode_reconnect_response(response)
    assert encoded["type"] == "reconnect_response"
    assert encoded["results"][0]["batch"]["events"][0]["event_id"] == "e1"
    schema_wire = encode_client_schema(schema)
    assert schema_wire["schema"]["digest"] == schema.digest


def test_mobile_contract_preserves_continuation_and_snapshot_gap_signal():
    schema = _schema()
    stream = ResumableStream("s", capacity=2)
    stream.publish("message", {}, event_id="e1")
    stream.publish("message", {}, event_id="e2")
    first = ClientParityContract(schema, {"s": stream}).reconnect(
        ReconnectRequest("flutter-1", schema.digest, (ResumeCursor("s", 0),), 1)
    )
    assert encode_reconnect_response(first)["results"][0]["batch"]["has_more"]
    with pytest.raises(MobileWireError):
        decode_reconnect_request({"type": "reconnect", "version": 1, "client_id": "x", "cursors": [{"stream_id": "s", "watermark": -1}]})
