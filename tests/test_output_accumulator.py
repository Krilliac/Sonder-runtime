import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from sonder_runtime.application.output_stream import (
    BoundedOutputAccumulator,
    ConflictingReplayError,
    InvalidSequenceError,
    OutputLimits,
    OutputStreamId,
    OutputStreamState,
    RevisionConflictError,
    TerminalStateError,
)


class RecordingEvents:
    def __init__(self):
        self.items = []

    def emit(self, event_code, **fields):
        self.items.append((event_code, fields))


def _accumulator(*, limits=None, redactor=lambda value: value, events=None):
    sink = events or RecordingEvents()
    return BoundedOutputAccumulator(
        OutputStreamId("out_" + "a" * 32),
        limits=limits,
        redactor=redactor,
        events=sink,
    ), sink


def test_chunks_are_monotonic_and_new_mutations_use_revision_cas():
    stream, _ = _accumulator()

    first = stream.append(0, "alpha", expected_revision=0)
    with pytest.raises(RevisionConflictError):
        stream.append(1, "beta", expected_revision=0)
    with pytest.raises(InvalidSequenceError):
        stream.append(2, "beta", expected_revision=1)
    second = stream.append(1, "beta", expected_revision=1)

    assert first.snapshot.revision == 1
    assert second.snapshot.revision == 2
    assert second.snapshot.next_sequence == 2
    assert second.snapshot.sha256 == hashlib.sha256(b"alphabeta").hexdigest()


def test_exact_replay_is_idempotent_and_conflicting_replay_is_rejected():
    stream, _ = _accumulator()
    accepted = stream.append(0, "same", expected_revision=0)
    replay = stream.append(0, "same", expected_revision=0)

    assert accepted.replayed is False
    assert replay.replayed is True
    assert replay.snapshot.revision == 1
    with pytest.raises(ConflictingReplayError):
        stream.append(0, "different", expected_revision=1)


@pytest.mark.parametrize(
    ("limits", "chunks", "failure_code"),
    [
        (OutputLimits(max_chunk_bytes=3, max_total_bytes=9), ["four"], "CHUNK_BYTES_LIMIT"),
        (
            OutputLimits(max_chunk_bytes=4, max_total_bytes=6),
            ["four", "xyz"],
            "TOTAL_BYTES_LIMIT",
        ),
        (
            OutputLimits(max_chunk_bytes=4, max_total_bytes=8, max_chunks=1),
            ["one", "two"],
            "CHUNK_COUNT_LIMIT",
        ),
    ],
)
def test_caps_fail_terminally_without_accepting_the_offending_chunk(
    limits, chunks, failure_code,
):
    events = RecordingEvents()
    stream, _ = _accumulator(limits=limits, events=events)
    for sequence, chunk in enumerate(chunks[:-1]):
        stream.append(sequence, chunk, expected_revision=sequence)

    before = stream.snapshot()
    with pytest.raises(TerminalStateError, match="limit exceeded"):
        stream.append(len(chunks) - 1, chunks[-1], expected_revision=before.revision)
    after = stream.snapshot()

    assert after.state is OutputStreamState.FAILED
    assert after.failure_code == failure_code
    assert after.chunk_count == before.chunk_count
    assert after.total_bytes == before.total_bytes
    assert after.revision == before.revision + 1
    assert events.items[-1][0] == "OUTPUT_STREAM_FAILED"


def test_finalize_and_fail_are_explicit_idempotent_terminal_transitions():
    finalized, events = _accumulator()
    finalized.append(0, "done", expected_revision=0)
    first = finalized.finalize(expected_revision=1)
    replay = finalized.finalize(expected_revision=0)

    assert replay == first
    assert first.state is OutputStreamState.FINALIZED
    with pytest.raises(TerminalStateError):
        finalized.append(1, "late", expected_revision=2)
    with pytest.raises(TerminalStateError):
        finalized.fail("UPSTREAM_FAILED", expected_revision=2)
    assert len(events.items) == 1

    failed, _ = _accumulator()
    failure = failed.fail("UPSTREAM_FAILED", expected_revision=0)
    assert failed.fail("UPSTREAM_FAILED", expected_revision=99) == failure
    with pytest.raises(TerminalStateError):
        failed.fail("OTHER_FAILURE", expected_revision=1)
    with pytest.raises(TerminalStateError):
        failed.finalize(expected_revision=1)


def test_utf8_limits_never_emit_a_partial_character_and_digest_exact_bytes():
    stream, _ = _accumulator(
        limits=OutputLimits(
            max_chunk_bytes=8,
            max_total_bytes=8,
            preview_bytes=5,
        )
    )

    snapshot = stream.append(0, "ééé", expected_revision=0).snapshot

    assert snapshot.total_bytes == 6
    assert snapshot.preview == "éé"
    assert snapshot.preview_truncated is True
    assert snapshot.sha256 == hashlib.sha256("ééé".encode("utf-8")).hexdigest()
    with pytest.raises(ValueError, match="Unicode"):
        stream.append(1, "\udcff", expected_revision=1)


def test_preview_is_redacted_bounded_and_never_written_to_terminal_event():
    events = RecordingEvents()
    stream, _ = _accumulator(
        limits=OutputLimits(max_chunk_bytes=64, max_total_bytes=64, preview_bytes=32),
        redactor=lambda value: value.replace("token=hunter2", "token=<redacted>"),
        events=events,
    )
    stream.append(0, "token=hunter2 result", expected_revision=0)
    snapshot = stream.finalize(expected_revision=1)

    assert "hunter2" not in snapshot.preview
    event_text = repr(events.items)
    assert "hunter2" not in event_text
    assert "redacted" not in event_text
    assert events.items[0][1]["detail"]["sha256"] == snapshot.sha256


def test_redactor_sees_lookahead_when_credential_crosses_display_boundary():
    raw = "prefix postgres://admin:S3cr3tPw@db.internal/result"
    display_bytes = raw.index("3tPw")

    def redact(value):
        return value.replace(
            "postgres://admin:S3cr3tPw@", "postgres://<redacted>@",
        )

    stream, _ = _accumulator(
        limits=OutputLimits(
            max_chunk_bytes=128, max_total_bytes=128,
            preview_bytes=display_bytes,
        ),
        redactor=redact,
    )
    snapshot = stream.append(0, raw, expected_revision=0).snapshot

    assert "admin" not in snapshot.preview
    assert "S3cr" not in snapshot.preview
    assert "<redacted>" in snapshot.preview
    assert snapshot.preview_truncated is True


def test_redactor_failure_fails_preview_closed():
    def broken_redactor(_value):
        raise RuntimeError("secret from redactor")

    stream, _ = _accumulator(redactor=broken_redactor)
    snapshot = stream.append(0, "sensitive", expected_revision=0).snapshot

    assert snapshot.preview == "<preview redaction failed>"


def test_blocked_redactor_does_not_hold_stream_lock():
    entered = threading.Event()
    release = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def redactor(value):
        nonlocal calls
        with calls_lock:
            calls += 1
            call = calls
        if call == 1:
            entered.set()
            assert release.wait(2)
        return value

    stream, _ = _accumulator(redactor=redactor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        blocked_snapshot = pool.submit(stream.snapshot)
        assert entered.wait(1)
        append = pool.submit(stream.append, 0, "progress", expected_revision=0)
        assert append.result(timeout=1).snapshot.revision == 1
        release.set()
        assert blocked_snapshot.result(timeout=1).revision == 0


def test_concurrent_same_sequence_has_one_winner_and_exact_replay():
    stream, _ = _accumulator()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(
            lambda _index: stream.append(0, "same", expected_revision=0),
            range(2),
        ))

    assert sorted(result.replayed for result in results) == [False, True]
    assert stream.snapshot().chunk_count == 1
    assert stream.snapshot().revision == 1


def test_concurrent_conflicting_replay_cannot_overwrite_winner():
    stream, _ = _accumulator()

    def attempt(value):
        try:
            return stream.append(0, value, expected_revision=0).snapshot.preview
        except ConflictingReplayError:
            return "conflict"

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, ("left", "right")))

    assert results.count("conflict") == 1
    assert stream.snapshot().preview in {"left", "right"}
    assert stream.snapshot().chunk_count == 1


def test_event_sink_failure_does_not_change_terminal_success():
    class RaisingEvents:
        def emit(self, *_args, **_kwargs):
            raise RuntimeError("operations store unavailable")

    stream, _ = _accumulator(events=RaisingEvents())
    stream.append(0, "done", expected_revision=0)
    snapshot = stream.finalize(expected_revision=1)

    assert snapshot.state is OutputStreamState.FINALIZED
    assert stream.snapshot() == snapshot


def test_append_racing_finalize_has_one_cas_winner_and_consistent_state():
    stream, _ = _accumulator()
    stream.append(0, "first", expected_revision=0)

    def append():
        try:
            return ("append", stream.append(1, "second", expected_revision=1))
        except (RevisionConflictError, TerminalStateError) as exc:
            return ("append-error", exc)

    def finalize():
        try:
            return ("finalize", stream.finalize(expected_revision=1))
        except RevisionConflictError as exc:
            return ("finalize-error", exc)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [pool.submit(append), pool.submit(finalize)]
        outcomes = [future.result() for future in results]

    assert sum(name in {"append", "finalize"} for name, _ in outcomes) == 1
    snapshot = stream.snapshot()
    if snapshot.state is OutputStreamState.OPEN:
        assert snapshot.chunk_count == 2
        snapshot = stream.finalize(expected_revision=snapshot.revision)
    else:
        assert snapshot.state is OutputStreamState.FINALIZED
        assert snapshot.chunk_count == 1
    assert snapshot.sha256 in {
        hashlib.sha256(b"first").hexdigest(),
        hashlib.sha256(b"firstsecond").hexdigest(),
    }


def test_terminal_stream_retains_replay_metadata_but_releases_raw_preview_source():
    stream, _ = _accumulator()
    stream.append(0, "sensitive raw output", expected_revision=0)
    stream.finalize(expected_revision=1)

    assert stream._preview_source == bytearray()
    length, digest = stream._chunks[0]
    assert length == len(b"sensitive raw output")
    assert digest == hashlib.sha256(b"sensitive raw output").digest()
    assert b"sensitive raw output" not in repr(stream._chunks).encode("utf-8")
