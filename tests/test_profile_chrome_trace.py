"""Chrome trace JSON: zones per thread, frame statistics, spikes and bounds."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.fixtures.profiling.bounded_json_double import install

install()

from sonder_runtime.domain.profiling.chrome_trace import (  # noqa: E402
    PERFETTO_HINT,
    looks_like_perfetto_protobuf,
    parse_chrome_trace,
)
from sonder_runtime.domain.profiling.model import (  # noqa: E402
    ProfileFormatUnknown,
    ProfileLimits,
    ProfileParseError,
)
from sonder_runtime.domain.profiling.render import digest_to_wire  # noqa: E402

FIXTURE = Path(__file__).parent / "fixtures" / "profiling" / "chrome_trace_frames.json"
SPIKE_FRAMES = (30, 60, 90)
SLOW_FRAME = 45


def build_frame_trace() -> dict:
    """3 threads, nested zones, 120 frames at 16.6+-0.3 ms, 3 x 40 ms spikes, one 18 ms frame.

    Deterministic; the committed fixture must equal this output.
    """
    events = [
        {"ph": "M", "pid": 1, "tid": 1, "name": "thread_name", "args": {"name": "MainThread"}},
        {"ph": "M", "pid": 1, "tid": 2, "name": "thread_name", "args": {"name": "Worker1"}},
        {"ph": "M", "pid": 1, "tid": 3, "name": "thread_name", "args": {"name": "Audio"}},
        {"ph": "M", "pid": 1, "tid": 1, "name": "process_name", "args": {"name": "spark_game"}},
    ]
    ts = 1000.0  # microseconds
    for index in range(120):
        frame = 16600.0 + ((index * 37) % 7 - 3) * 100.0  # 16.3 .. 16.9 ms
        if index in SPIKE_FRAMES:
            frame = 40000.0
        elif index == SLOW_FRAME:
            frame = 18000.0
        physics = frame - 9000.0
        events.append({"ph": "X", "pid": 1, "tid": 1, "name": "Frame", "ts": ts, "dur": frame})
        events.append({"ph": "X", "pid": 1, "tid": 1, "name": "Update", "ts": ts + 50,
                       "dur": physics + 1500})
        events.append({"ph": "X", "pid": 1, "tid": 1, "name": "Physics::Integrate",
                       "ts": ts + 100, "dur": physics})
        events.append({"ph": "X", "pid": 1, "tid": 1, "name": "AI::Think",
                       "ts": ts + physics + 200, "dur": 1000})
        events.append({"ph": "X", "pid": 1, "tid": 1, "name": "Render",
                       "ts": ts + physics + 1700, "dur": 6000})
        events.append({"ph": "X", "pid": 1, "tid": 2, "name": "Job::Decode", "ts": ts + 300,
                       "dur": 4000})
        events.append({"ph": "B", "pid": 1, "tid": 3, "name": "Audio::Mix", "ts": ts + 500})
        events.append({"ph": "B", "pid": 1, "tid": 3, "name": "Audio::Resample", "ts": ts + 600})
        events.append({"ph": "E", "pid": 1, "tid": 3, "ts": ts + 1600})
        events.append({"ph": "E", "pid": 1, "tid": 3, "ts": ts + 2500})
        ts += frame
    return {"traceEvents": events, "displayTimeUnit": "ms"}


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def test_committed_fixture_matches_generator():
    assert json.loads(_fixture_text()) == build_frame_trace()


def _chunks(text: str, size: int = 4096):
    for start in range(0, len(text), size):
        yield text[start:start + size]


def test_frame_stats_and_spikes_with_mad_floor():
    digest = parse_chrome_trace(_chunks(_fixture_text()), frame_budget_ms=16.6)
    frames = digest.frames
    assert frames is not None and frames.count == 120
    assert frames.max_ms == pytest.approx(40.0)
    # 116 normal frames in 16.3..16.9 plus 18 and 3x40: nearest-rank values.
    assert frames.p99_ms == pytest.approx(40.0)
    assert frames.p95_ms == pytest.approx(16.9)
    assert frames.budget_ms == pytest.approx(16.6)
    flagged = {spike.start_ns for spike in digest.spikes}
    assert len(digest.spikes) == 3
    assert all(spike.duration_ns == 40_000_000 for spike in digest.spikes)
    assert all(spike.thread == "MainThread" for spike in digest.spikes)
    # The 18 ms frame is NOT a spike (MAD floor regression test).
    assert all(spike.duration_ns != 18_000_000 for spike in digest.spikes)
    assert len(flagged) == 3
    assert digest.metadata.process == "spark_game"
    assert digest.metadata.threads == 3


def test_zone_self_total_and_hot_paths():
    digest = parse_chrome_trace([_fixture_text()])
    top_self = {fn.name: fn for fn in digest.top_self}
    top_total = {fn.name: fn for fn in digest.top_total}
    assert digest.top_self[0].name == "Physics::Integrate"
    # Update contains Physics::Integrate and AI::Think; Frame contains Update and Render.
    assert top_total["Frame"].total_value >= top_total["Update"].total_value
    assert top_total["Update"].total_value == (top_self["Update"].self_value
                                               + top_total["Physics::Integrate"].total_value
                                               + top_total["AI::Think"].total_value)
    assert top_total["AI::Think"].total_value == 120 * 1_000_000
    assert "Audio::Resample" in top_self and top_self["Audio::Resample"].self_value == 120 * 1_000_000
    assert top_total["Audio::Mix"].total_value == 120 * 2_000_000
    assert digest.hot_paths[0].frames == ("Frame", "Update", "Physics::Integrate")
    values = [path.value for path in digest.hot_paths]
    assert values == sorted(values, reverse=True)


def test_thread_filter_by_name_and_tid():
    by_name = parse_chrome_trace([_fixture_text()], thread="Worker1")
    assert [fn.name for fn in by_name.top_self] == ["Job::Decode"]
    by_tid = parse_chrome_trace([_fixture_text()], thread="3")
    assert {fn.name for fn in by_tid.top_self} == {"Audio::Mix", "Audio::Resample"}


def test_bare_array_and_bytes_chunks():
    events = build_frame_trace()["traceEvents"]
    text = json.dumps(events)
    digest = parse_chrome_trace([text.encode("utf-8")[:10], text.encode("utf-8")[10:]])
    assert digest.frames is not None and digest.frames.count == 120


def test_instant_frame_markers_give_frames():
    events = [{"ph": "I", "name": "Frame", "ts": 1000.0 + 16600.0 * i, "pid": 1, "tid": 1, "s": "g"}
              for i in range(50)]
    events.append({"ph": "X", "name": "Work", "ts": 0, "dur": 10, "pid": 1, "tid": 1})
    digest = parse_chrome_trace([json.dumps({"traceEvents": events})])
    assert digest.frames is not None and digest.frames.count == 49
    assert digest.frames.p50_ms == pytest.approx(16.6)
    assert digest.spikes == ()


def test_configurable_frame_zone():
    digest = parse_chrome_trace([_fixture_text()], frame_zone="Render")
    assert digest.frames is not None and digest.frames.count == 120
    assert digest.frames.max_ms == pytest.approx(6.0)
    assert digest.spikes == ()


def test_perfetto_protobuf_is_refused_with_traceconv_hint():
    protobuf = bytes([0x0A, 0x8C, 0x01, 0x08, 0x01, 0x10, 0x02]) + b"\x00" * 32
    assert looks_like_perfetto_protobuf(protobuf)
    with pytest.raises(ProfileFormatUnknown) as caught:
        parse_chrome_trace([protobuf])
    assert "traceconv" in caught.value.hint
    assert caught.value.hint == PERFETTO_HINT[:240]


def test_not_json_is_unknown_and_empty_trace_is_parse_error():
    with pytest.raises(ProfileFormatUnknown):
        parse_chrome_trace(["hello"])
    with pytest.raises(ProfileParseError):
        parse_chrome_trace(['{"traceEvents": []}'])


def test_oversize_event_is_skipped_and_counted():
    big = {"ph": "X", "name": "Huge", "ts": 0, "dur": 5, "pid": 1, "tid": 1,
           "args": {"blob": "x" * (1 << 20)}}
    small = {"ph": "X", "name": "Small", "ts": 10, "dur": 5, "pid": 1, "tid": 1}
    text = json.dumps({"traceEvents": [big, small]})
    digest = parse_chrome_trace(_chunks(text, 65536))
    assert [fn.name for fn in digest.top_self] == ["Small"]
    assert any("skipped" in note for note in digest.notes)


def test_event_cap_sets_truncated():
    events = [{"ph": "X", "name": "Z%d" % (i % 5), "ts": i * 10, "dur": 5, "pid": 1, "tid": 1}
              for i in range(200)]
    digest = parse_chrome_trace([json.dumps(events)], ProfileLimits(max_events=50))
    assert digest.truncated
    assert digest.metadata.sample_count == 50


def test_unterminated_and_malformed_events_are_noted():
    events = [
        {"ph": "B", "name": "Open", "ts": 0, "pid": 1, "tid": 1},
        {"ph": "E", "ts": 5, "pid": 9, "tid": 9},
        {"ph": "X", "name": "Bad", "ts": "soon", "dur": 3, "pid": 1, "tid": 1},
        {"ph": "X", "name": "Nan", "ts": 1, "dur": -3, "pid": 1, "tid": 1},
        {"ph": "X", "name": "Good", "ts": 1, "dur": 3, "pid": 1, "tid": 1},
        7,
    ]
    digest = parse_chrome_trace([json.dumps(events)])
    assert [fn.name for fn in digest.top_self] == ["Good"]
    assert any("without a matching E" in note for note in digest.notes)
    assert any("malformed" in note for note in digest.notes)


def test_wire_payload_is_bounded():
    digest = parse_chrome_trace([_fixture_text()], frame_budget_ms=16.6)
    payload = digest_to_wire(digest)
    assert len(json.dumps(payload, separators=(",", ":")).encode("utf-8")) <= 48_000


def test_lane_a_bounds_error_outside_the_array_is_a_typed_parse_error():
    # Lane A's JsonBoundsExceeded is an InvalidInput, not a ValueError: nesting
    # deeper than 64 before traceEvents must still surface as ProfileParseError.
    bomb = '{"meta":' + "[" * 200 + "]" * 200 + ',"traceEvents":[{"ph":"X","name":"a","ts":1,"dur":1}]}'
    with pytest.raises(ProfileParseError):
        parse_chrome_trace([bomb])


def test_undecodable_events_are_counted_separately():
    text = ('{"traceEvents":[{"ph":"X","name":"ok","ts":1,"dur":2},'
            '{"ph":"X","name":"bad","ts":01,"dur":2},[1,2],{"ph":"X","name":"b2","ts":1,"dur":2,}]}')
    digest = parse_chrome_trace([text])
    assert [fn.name for fn in digest.top_self] == ["ok"]
    assert any(note.startswith("3 undecodable") for note in digest.notes), digest.notes


def test_utf8_bom_object_form_is_read():
    body = json.dumps({"traceEvents": [{"ph": "X", "name": "a", "ts": 1, "dur": 3}]})
    for chunks in (["\ufeff" + body], [b"\xef\xbb\xbf" + body.encode()], ["\ufeff", " \n", body]):
        assert [fn.name for fn in parse_chrome_trace(chunks).top_self] == ["a"]
