import pytest

from sonder_runtime.application.protocol.resumable_streams import ResumableStream, StreamBackpressure
from sonder_runtime.domain.protocol.events import EventEnvelope, validate_monotonic


def test_typed_events_require_monotonic_sequences():
    first = EventEnvelope("s", 1, "created", {"x": 1}, "e1")
    second = EventEnvelope("s", 2, "updated", {"x": 2}, "e2")
    validate_monotonic([first, second])
    with pytest.raises(ValueError):
        validate_monotonic([second, first])


def test_snapshot_and_resume_watermark_replay():
    stream = ResumableStream("s", capacity=4)
    stream.publish("created", {}, event_id="e1")
    stream.publish("updated", {}, event_id="e2")
    stream.publish_snapshot({"ready": True})
    stream.publish("finished", {}, event_id="e3")
    batch = stream.resume(0)
    assert batch.snapshot is not None and batch.snapshot.watermark == 2
    assert [event.sequence for event in batch.events] == [3]


def test_duplicate_suppression_and_bounded_backpressure():
    stream = ResumableStream("s", capacity=1)
    first = stream.publish("created", {}, event_id="same")
    assert stream.publish("created", {"ignored": True}, event_id="same") == first
    with pytest.raises(StreamBackpressure):
        stream.publish("updated", {}, event_id="second")


def test_resume_limit_and_watermark_validation():
    stream = ResumableStream("s", capacity=2)
    stream.publish("a", {}, event_id="a")
    stream.publish("b", {}, event_id="b")
    batch = stream.resume(0, limit=1)
    assert batch.has_more and batch.next_watermark == 1
    assert [event.sequence for event in stream.resume(batch.next_watermark).events] == [2]
    with pytest.raises(ValueError):
        stream.resume(99)
