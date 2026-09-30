"""LiveTurnStream: what a streamed chat turn forwards, holds back and revises."""
from __future__ import annotations

from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.chat.stream_sink import REVISION_NOTICE, LiveTurnStream


def _stream(**kwargs):
    sent: list[str] = []
    stream = LiveTurnStream(lambda text: sent.append(text) or True, **kwargs)
    return stream, sent


def test_deltas_are_forwarded_in_order():
    stream, sent = _stream()
    for piece in ("Hel", "lo", " world"):
        assert stream.emit(piece) is True
    assert sent == ["Hel", "lo", " world"]
    assert stream.forwarded == "Hello world"
    assert stream.ttft_ms() is not None


def test_code_is_held_back_from_the_first_fence_even_when_split():
    stream, sent = _stream(hold_marker="```")
    for piece in ("Here is code:\n", "`", "``py", "thon\nprint(1)\n```\n", "done"):
        stream.emit(piece)
    assert "".join(sent) == "Here is code:\n"
    assert stream.held is True
    assert stream.generated.endswith("done")


def test_a_lone_backtick_is_released_once_it_is_not_a_fence():
    stream, sent = _stream(hold_marker="```")
    stream.emit("use `")
    stream.emit("x` here")
    assert "".join(sent) == "use `x` here"
    assert stream.held is False


def test_reconcile_sends_only_the_remainder_of_a_continuation():
    stream, _sent = _stream(hold_marker="```")
    stream.emit("Prose. ```code```")
    assert stream.reconcile("Prose. ```code```\n\ntrace") == ("```code```\n\ntrace", False)


def test_reconcile_ignores_trailing_whitespace_only_differences():
    stream, _sent = _stream()
    stream.emit("Answer.\n\n")
    assert stream.reconcile("Answer.") == ("", False)


def test_reconcile_announces_a_revision_instead_of_dropping_it():
    stream, _sent = _stream()
    stream.emit("First draft")
    rest, revised = stream.reconcile("Repaired answer")
    assert revised is True
    assert REVISION_NOTICE in rest and rest.endswith("Repaired answer")


def test_nothing_forwarded_means_the_whole_answer_is_still_to_send():
    stream, _sent = _stream()
    assert stream.reconcile("full") == ("full", False)


def test_a_failed_write_marks_the_client_gone():
    stream = LiveTurnStream(lambda text: False)
    assert stream.emit("x") is False
    assert stream.cancelled is True
    assert stream.emit("y") is False


def test_client_gone_callback_cancels_without_writing():
    sent = []
    stream = LiveTurnStream(lambda text: sent.append(text) or True, client_gone=lambda: True)
    assert stream.emit("x") is False
    assert sent == []


def test_only_one_call_claims_an_armed_stream_and_scopes_reset():
    stream, _sent = _stream()
    assert stream_sink.call_stream() is None
    with stream_sink.armed(stream):
        assert stream_sink.turn_stream() is stream
        with stream_sink.claimed_for_call() as first:
            assert first is stream and stream_sink.call_stream() is stream
        assert stream_sink.call_stream() is None
        with stream_sink.claimed_for_call() as second:
            assert second is None and stream_sink.call_stream() is None
    assert stream_sink.turn_stream() is None


def test_nothing_armed_claims_nothing():
    with stream_sink.claimed_for_call() as claimed:
        assert claimed is None
