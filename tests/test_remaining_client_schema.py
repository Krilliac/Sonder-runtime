"""Focused proof for API-007/008 client parity and schema freshness."""
from __future__ import annotations

from sonder_runtime.application.ports.tool_registry import InMemoryToolRegistry, ToolDescriptor
from sonder_runtime.application.protocol.client_schema import (
    ClientParityContract,
    ReconnectRequest,
    ResumeCursor,
    ResumeDisposition,
    SchemaFreshness,
    build_client_schema,
    check_schema_freshness,
)
from sonder_runtime.application.protocol.resumable_streams import ResumableStream
from sonder_runtime.application.tools.generated_catalogs import GeneratedCatalogs


def _schema():
    registry = InMemoryToolRegistry((
        ToolDescriptor("read_file", "Read a bounded file", {"type": "object"}),
        ToolDescriptor("status", "Read runtime status", {"type": "object"}),
    ))
    catalogs = GeneratedCatalogs.generate(registry, commands=("status", "help"))
    return build_client_schema(catalogs)


def test_schema_is_runtime_derived_and_digest_is_stable():
    first = _schema()
    second = _schema()
    assert first.digest == second.digest
    assert first.source_catalog_digest
    assert first.client["tools"][0]["name"] == "read_file"
    assert first.sdk["commands"][0]["name"] == "help"
    assert first.as_dict()["digest"] == first.digest


def test_schema_digest_covers_stream_contract_and_detects_freshness():
    default = _schema()
    changed = build_client_schema(
        GeneratedCatalogs.generate(InMemoryToolRegistry((ToolDescriptor("status"),))),
        stream_contract={"batch_limit": 32},
    )
    assert default.digest != changed.digest
    assert check_schema_freshness(default.digest, default).state is SchemaFreshness.CURRENT
    assert check_schema_freshness("not-a-digest", default).state is SchemaFreshness.INVALID
    assert check_schema_freshness(changed.digest, default).state is SchemaFreshness.STALE


def test_reconnect_resumes_from_watermark_with_bounded_batch():
    schema = _schema()
    stream = ResumableStream("session-1", capacity=8)
    stream.publish("message", {"text": "one"}, event_id="e1")
    stream.publish("message", {"text": "two"}, event_id="e2")
    response = ClientParityContract(schema, {"session-1": stream}).reconnect(
        ReconnectRequest("flutter-device", schema.digest, (ResumeCursor("session-1", 0),), 1)
    )
    result = response.results[0]
    assert response.resumed
    assert result.disposition is ResumeDisposition.RESUMED
    assert result.batch is not None and [event.event_id for event in result.batch.events] == ["e1"]
    assert result.batch.has_more


def test_reconnect_requires_schema_refresh_before_replay():
    schema = _schema()
    stream = ResumableStream("session-1")
    stream.publish("message", {}, event_id="e1")
    response = ClientParityContract(schema, {"session-1": stream}).reconnect(
        ReconnectRequest("mobile", "0" * 64, (ResumeCursor("session-1", 0),))
    )
    assert response.freshness.state is SchemaFreshness.STALE
    assert response.results[0].disposition is ResumeDisposition.REFRESH_SCHEMA
    assert response.results[0].batch is None


def test_reconnect_requests_snapshot_for_retention_gap():
    schema = _schema()
    stream = ResumableStream("session-1", capacity=1)
    stream.publish("message", {}, event_id="e1")
    stream.publish_snapshot({"state": "checkpoint"})
    stream.publish("message", {}, event_id="e2")
    # A watermark before the retained history is recoverable through the
    # snapshot, so the result is still a valid replay rather than a lie.
    response = ClientParityContract(schema, {"session-1": stream}).reconnect(
        ReconnectRequest("mobile", schema.digest, (ResumeCursor("session-1", 0),))
    )
    assert response.results[0].disposition is ResumeDisposition.RESUMED
    assert response.results[0].batch is not None
    assert response.results[0].batch.snapshot is not None


def test_reconnect_unknown_stream_is_explicitly_rejected():
    schema = _schema()
    response = ClientParityContract(schema, {}).reconnect(
        ReconnectRequest("mobile", schema.digest, (ResumeCursor("gone", 0),))
    )
    assert response.results[0].disposition is ResumeDisposition.REJECTED
