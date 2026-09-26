"""Observatory live producer: envelopes, bounded ring, resume and discovery."""
import json
import re
import threading
import time
import tracemalloc
from datetime import datetime, timezone

import pytest

from sonder_runtime.adapters.observability.observatory_producer import (
    DISCOVERY_SCHEMA,
    EVENT_SCHEMA,
    ObservatoryProducer,
    clamp_buffer,
    parse_event_id,
    rfc3339_millis,
)
from sonder_runtime.application.ports.telemetry_feed import SubscriberLimitReached
from sonder_runtime.application.ports.telemetry_sink import TelemetryEvent

REQUIRED = ("schema", "event_id", "sequence", "event_type", "wall_time", "mono_ns",
            "session_id", "producer", "attributes")
WALL_TIME = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def _event(code="request.started", **fields):
    return TelemetryEvent(code, datetime.now(timezone.utc), fields or {"n": 1},
                          correlation_id="req-1", run_id="req-1")


def _producer(**kwargs):
    kwargs.setdefault("version", "0.9.0")
    kwargs.setdefault("node_id", "test-host")
    kwargs.setdefault("instance_hex", "0123456789ab")
    return ObservatoryProducer(**kwargs)


def _lines(subscription, limit=4096):
    return [json.loads(event.line) for event in subscription.next_batch(0.05, limit=limit).events]


def test_envelope_satisfies_the_observatory_event_schema():
    producer = _producer()
    for _ in range(3):
        producer.emit(_event())
    envelopes = _lines(producer.subscribe())
    assert [e["sequence"] for e in envelopes] == [0, 1, 2]
    for envelope in envelopes:
        for key in REQUIRED:
            assert key in envelope
        assert envelope["schema"] == EVENT_SCHEMA
        assert envelope["event_id"] == "rt-0123456789ab-%d" % envelope["sequence"]
        assert WALL_TIME.match(envelope["wall_time"])
        assert type(envelope["mono_ns"]) is int and envelope["mono_ns"] >= 0
        assert envelope["session_id"] == "rts-0123456789ab"
        assert envelope["request_id"] == envelope["run_id"] == "req-1"
        assert envelope["producer"] == {
            "name": "sonder-runtime", "version": "0.9.0", "node_id": "test-host",
            "instance_id": "rt-0123456789ab", "role": "runtime", "synthetic": False,
        }
        assert envelope["sampling"] == {"level": "metrics", "sampled": True}


def test_mono_ns_is_the_host_monotonic_clock():
    before = time.monotonic_ns()
    producer = ObservatoryProducer(version="v", node_id="h")
    producer.emit(_event())
    after = time.monotonic_ns()
    envelope = _lines(producer.subscribe())[0]
    assert before <= envelope["mono_ns"] <= after
    assert re.fullmatch(r"rt-[0-9a-f]{12}", producer.instance_id)
    assert producer.session_id == "rts-" + producer.instance_id[3:]


def test_rfc3339_millis_is_utc_with_z():
    moment = datetime(2026, 9, 26, 10, 11, 12, 345678, tzinfo=timezone.utc)
    assert rfc3339_millis(moment) == "2026-09-26T10:11:12.345Z"


def test_operation_id_stands_in_for_run_id():
    producer = _producer()
    producer.emit(TelemetryEvent("x.y", datetime.now(timezone.utc), {},
                                 correlation_id="r", operation_id="op-1"))
    assert _lines(producer.subscribe())[0]["run_id"] == "op-1"


def test_emits_without_subscribers_stay_bounded_in_memory():
    """Memory is bounded by the ring, not by the number of emits.

    Measured with tracemalloc (which slows every allocation), so this test
    makes no timing claim; the cost bound is the separate test below.
    """
    producer = _producer(capacity=4096)
    tracemalloc.start()
    try:
        for index in range(8192):  # fill the ring twice
            producer.emit(_event(n=index))
        filled, _peak = tracemalloc.get_traced_memory()
        for index in range(20_000):
            producer.emit(_event(n=index))
        after, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    stats = producer.stats()
    assert stats["emitted_events"] == 28_192
    assert stats["retained_events"] == 4096
    assert stats["dropped_events"] == 0
    # 20k further emits into a full ring retain nothing new.
    assert after - filled < 512 * 1024
    assert peak < 16 * 1024 * 1024


def test_hundred_thousand_emits_without_subscribers_are_cheap():
    """Constant emit cost, with a generous bound that survives a loaded runner.

    Measured without tracemalloc; typical cost is tens of microseconds.
    """
    producer = _producer(capacity=4096)
    started = time.perf_counter()
    for index in range(100_000):
        producer.emit(_event(n=index))
    elapsed = time.perf_counter() - started
    assert producer.stats()["emitted_events"] == 100_000
    assert elapsed / 100_000 < 0.001  # under 1 ms per emit on average


def test_sequence_and_mono_ns_orderings_agree_under_concurrency():
    """mono_ns is stamped under the lock that assigns sequence (replay order)."""
    producer = _producer(capacity=65536)

    def emit_many():
        for index in range(2_000):
            producer.emit(_event(n=index))

    workers = [threading.Thread(target=emit_many) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
    subscription = producer.subscribe()
    envelopes = []
    while True:
        batch = subscription.next_batch(0.05, limit=4096)
        if not batch.events:
            break
        envelopes.extend(json.loads(event.line) for event in batch.events)
    assert len(envelopes) == 16_000
    by_sequence = [e["mono_ns"] for e in sorted(envelopes, key=lambda e: e["sequence"])]
    assert by_sequence == sorted(by_sequence)


def test_an_unreadable_clock_drops_the_event_before_it_is_numbered():
    calls = iter([1, RuntimeError("clock"), 3, 4])

    def clock():
        value = next(calls)
        if isinstance(value, Exception):
            raise value
        return value

    producer = _producer(monotonic_ns=clock)
    producer.emit(_event())
    producer.emit(_event())  # the clock fails: dropped, reported, never raised
    envelopes = _lines(producer.subscribe())
    assert [e["sequence"] for e in envelopes] == [0, 1]
    assert envelopes[1]["event_type"] == "telemetry.dropped"
    assert envelopes[1]["attributes"]["dropped_events"] == 1


def test_closed_subscriptions_still_receive_events_sequenced_before_the_close():
    producer = _producer()
    subscription = producer.subscribe(since_now=True)
    producer.emit(_event("session.ended"))
    producer.close_subscribers()
    producer.emit(_event("after.close"))
    batch = subscription.next_batch(0.05)
    assert [json.loads(e.line)["event_type"] for e in batch.events] == ["session.ended"]
    assert subscription.next_batch(0.05).closed
    assert producer.stats()["subscribers"] == 0


def test_a_self_closed_subscription_drains_nothing():
    producer = _producer()
    subscription = producer.subscribe()
    producer.emit(_event())
    subscription.close()
    assert subscription.next_batch(0.05).closed


def test_a_stalled_subscriber_never_blocks_emit_and_learns_its_loss():
    producer = _producer(capacity=256)
    stalled = producer.subscribe()
    done = threading.Event()

    def emit_many():
        for index in range(5_000):
            producer.emit(_event(n=index))
        done.set()

    worker = threading.Thread(target=emit_many)
    worker.start()
    assert done.wait(10), "emit blocked behind a subscriber that never reads"
    worker.join(5)
    batch = stalled.next_batch(0.05, limit=4096)
    assert batch.lost == 5_000 - 256
    assert batch.events[0].sequence == 5_000 - 256
    stats = producer.stats()
    assert stats["subscriber_dropped_events"] == 5_000 - 256
    # A subscriber's loss is not a producer drop and is never published.
    assert stats["dropped_events"] == 0
    assert all("telemetry.dropped" not in e.line for e in batch.events)


def test_unserializable_events_are_dropped_counted_and_reported():
    producer = _producer()
    producer.emit(_event(bad=object()))
    producer.emit(_event(nan=float("nan")))
    envelopes = _lines(producer.subscribe())
    assert [e["event_type"] for e in envelopes] == ["telemetry.dropped", "telemetry.dropped"]
    assert [e["attributes"]["dropped_events"] for e in envelopes] == [1, 2]
    assert envelopes[-1]["attributes"] == {
        "dropped_events": 2, "emitted_events": 1, "queue_capacity": 4096, "final": False,
    }
    # Dropped events take no sequence number.
    assert [e["sequence"] for e in envelopes] == [0, 1]
    producer.report_final()
    assert _lines(producer.subscribe())[-1]["attributes"]["final"] is True


def test_resume_cases_follow_the_protocol():
    producer = _producer(capacity=256)
    for index in range(300):
        producer.emit(_event(n=index))
    oldest = 300 - 256
    same = producer.subscribe(last_event_id="rt-0123456789ab-100")
    assert same.resume_gap is None
    assert _lines(same)[0]["sequence"] == 101
    too_old = producer.subscribe(last_event_id="rt-0123456789ab-10")
    assert (too_old.resume_gap.first_missing, too_old.resume_gap.last_missing) == (11, oldest - 1)
    assert _lines(too_old)[0]["sequence"] == oldest
    other = producer.subscribe(last_event_id="rt-ffffffffffff-100")
    assert other.resume_gap is None and _lines(other)[0]["sequence"] == oldest
    fresh = producer.subscribe()
    assert _lines(fresh)[0]["sequence"] == oldest
    live = producer.subscribe(since_now=True)
    assert live.next_batch(0.01).events == ()
    producer.emit(_event())
    assert [e["sequence"] for e in _lines(live)] == [300]


def test_resume_from_the_newest_event_waits_for_the_next_one():
    producer = _producer()
    producer.emit(_event())
    subscription = producer.subscribe(last_event_id="rt-0123456789ab-0")
    assert subscription.next_batch(0.01).events == ()


def test_subscriber_cap_is_enforced_and_released():
    producer = _producer(max_subscribers=2)
    first = producer.subscribe()
    producer.subscribe()
    with pytest.raises(SubscriberLimitReached):
        producer.subscribe()
    first.close()
    producer.subscribe()


def test_close_subscribers_ends_open_streams_promptly():
    producer = _producer()
    subscription = producer.subscribe(since_now=True)
    results = []
    reader = threading.Thread(target=lambda: results.append(subscription.next_batch(30)))
    reader.start()
    time.sleep(0.05)
    producer.close_subscribers()
    reader.join(2)
    assert not reader.is_alive()
    assert results[0].closed is True
    assert producer.stats()["subscribers"] == 0


def test_discovery_document_matches_contract_section_5_1():
    producer = _producer()
    producer.emit(_event())
    document = producer.discovery(auth_required=False)
    assert document["schema"] == DISCOVERY_SCHEMA
    assert document["producer"] == {
        "name": "sonder-runtime", "version": "0.9.0", "node_id": "test-host",
        "instance_id": "rt-0123456789ab", "role": "runtime", "synthetic": False,
    }
    assert document["event_schema"] == EVENT_SCHEMA
    assert document["streams"] == [
        {"transport": "sse", "url": "/v1/observability/events"},
        {"transport": "ndjson", "url": "/v1/observability/events?format=ndjson"},
    ]
    assert document["resume"] == {
        "header": "Last-Event-ID", "query": "last_event_id",
        "retained_events": 1, "oldest_sequence": 0, "next_sequence": 1,
    }
    assert document["auth"] == {"required": False, "schemes": ["bearer"]}
    assert document["clock"] == {"mono_ns": "host-monotonic"}
    assert document["links"] == {"ecosystem": "/v1/sonder/ecosystem",
                                 "trace": "/v1/observability/trace"}
    assert document["vocabularies"] == {"sonder.runtime.events": 1}
    assert document["sampling_level"] == "metrics"
    assert document["text_capture"] == "none"
    assert _producer().discovery(auth_required=True)["resume"]["oldest_sequence"] is None


@pytest.mark.parametrize("raw,expected", [
    ("rt-0123456789ab-42", ("rt-0123456789ab", 42)),
    ("tel-a-b-7", ("tel-a-b", 7)),
    ("no-sequence-", None),
    ("plain", None),
    (None, None),
])
def test_event_ids_split_at_the_last_dash(raw, expected):
    assert parse_event_id(raw) == expected


@pytest.mark.parametrize("value,expected", [
    (10, 256), (4096, 4096), (10**6, 65536), ("bad", 4096),
])
def test_buffer_is_clamped(value, expected):
    assert clamp_buffer(value) == expected
