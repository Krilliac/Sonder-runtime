from datetime import datetime, timezone
from typing import get_type_hints

import pytest

from sonder_runtime.application.capabilities.observability import RedactingTelemetrySink
from sonder_runtime.application.ports import (
    ArtifactHandle, AttachmentStore, SpillHandle, SpillSnapshot, SpillSpec,
    SpillState, SpillStore, TelemetryEvent, TelemetrySink,
)


def test_artifact_handles_are_immutable_and_bounded_metadata():
    handle = ArtifactHandle("artifact-1", 3, "a" * 64, media_type="text/plain")
    assert handle.size_bytes == 3
    with pytest.raises((AttributeError, TypeError)):
        handle.size_bytes = 4  # type: ignore[misc]
    with pytest.raises(ValueError):
        ArtifactHandle("artifact-1", -1, "a" * 64)
    with pytest.raises(ValueError):
        ArtifactHandle("artifact-1", 1, "not-a-digest")


def test_spill_contract_has_explicit_bounded_lifecycle():
    spec = SpillSpec(1024, media_type="application/json", ttl_seconds=5)
    snapshot = SpillSnapshot("spill-1", SpillState.OPEN, 0, spec.max_bytes)
    assert snapshot.state is SpillState.OPEN
    assert snapshot.max_bytes == 1024
    with pytest.raises(ValueError):
        SpillSpec(0)
    assert {"begin", "reap"} <= set(vars(SpillStore))
    assert {"write", "commit", "abort", "close", "snapshot"} <= set(vars(SpillHandle))


def test_attachment_and_spill_ports_are_independent_protocols():
    assert AttachmentStore is not SpillStore
    assert get_type_hints(AttachmentStore.put)["return"] is ArtifactHandle
    assert get_type_hints(SpillStore.begin)["return"] is SpillHandle


class _Redactor:
    def redact(self, text):
        return text.replace("secret", "[REDACTED]")


class _Export:
    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


def test_telemetry_is_redacted_before_delegate_export():
    delegate = _Export()
    sink: TelemetrySink = RedactingTelemetrySink(delegate, _Redactor())
    event = TelemetryEvent(
        "artifact.created", datetime.now(timezone.utc),
        {"summary": "secret value", "nested": ["secret value"], "count": 1},
    )
    sink.emit(event)
    exported = delegate.events[0]
    assert exported.redaction_applied is True
    assert exported.fields["summary"] == "[REDACTED] value"
    assert exported.fields["nested"] == ["[REDACTED] value"]
    assert event.fields["summary"] == "secret value"


def test_telemetry_event_snapshots_fields_and_validates_identity():
    fields = {"count": 1}
    event = TelemetryEvent("metric.sampled", datetime.now(timezone.utc), fields)
    fields["count"] = 2
    assert event.fields["count"] == 1
    with pytest.raises(ValueError):
        TelemetryEvent("", datetime.now(timezone.utc))
    assert "emit" in vars(TelemetrySink)
