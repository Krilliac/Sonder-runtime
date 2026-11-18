"""Every frame of one streamed chat completion carries one ``id``.

``live_stream.arm`` minted its own id for the live token deltas, while the
remainder, the terminal frame and the usage frame of the same SSE response
used the turn's interaction id: one completion arrived as two
``chatcmpl-...`` ids. OpenAI-style clients key a streamed message by that
id, so the live deltas and the terminal frame were two different messages.
"""
from __future__ import annotations

import pytest

from sonder_runtime.application.chat import stream_sink
from tests.test_sonder_inference_streaming import TOKENS, FakeInference, _content, _served_turn

pytestmark = pytest.mark.unit


def _ids(events):
    return {event["id"] for event in events}


def test_live_deltas_and_terminal_frames_share_one_completion_id(monkeypatch):
    _first, _total, events, _text = _served_turn(monkeypatch, FakeInference(token_delay=0.0))
    content_events = [e for e in events
                      if e.get("choices") and e["choices"][0]["delta"].get("content")]
    assert len(content_events) > 1  # the deltas really were forwarded live
    assert _content(events) == "".join(TOKENS)
    assert [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert len(_ids(events)) == 1, _ids(events)


def test_revised_stream_keeps_the_live_completion_id(monkeypatch):
    import server

    monkeypatch.setattr(server, "_gate_answer_code",
                        lambda response, **kwargs: ("Repaired answer.", True, False, {}))
    _first, _total, events, _text = _served_turn(monkeypatch, FakeInference(token_delay=0.0))
    assert stream_sink.REVISION_NOTICE in _content(events)
    assert len(_ids(events)) == 1, _ids(events)


def test_error_after_live_deltas_keeps_the_live_completion_id(monkeypatch):
    _first, _total, events, _text = _served_turn(
        monkeypatch, FakeInference(token_delay=0.0, error_after=3))
    errors = [e for e in events if e.get("object") == "error"]
    deltas = [e for e in events if e.get("choices") and e["choices"][0]["delta"].get("content")]
    assert errors and deltas, events
    assert len(_ids(events)) == 1, _ids(events)


def test_stream_body_failure_after_live_deltas_keeps_the_live_completion_id(monkeypatch):
    """A write that fails after live deltas ends under the deltas' id, not the turn's."""
    import sonder_runtime.interfaces.http.serve as ts
    from sonder_runtime.interfaces.http import live_stream

    def failing_chunk(iid, model, delta, finish_reason=None, **kwargs):
        if finish_reason == "stop":
            raise RuntimeError("terminal frame failed")
        return ts._chunk(iid, model, delta, finish_reason=finish_reason, **kwargs)

    def body(self, iid, model, content, elapsed_ms, receipt, usage, activity, lock=None):
        return live_stream.write_stream_body(
            self, failing_chunk, iid, model, content, elapsed_ms, receipt, usage, activity,
            lock=lock)

    monkeypatch.setattr(ts.Handler, "_write_stream_body", body)
    _first, _total, events, _text = _served_turn(monkeypatch, FakeInference(token_delay=0.0))
    errors = [e for e in events if e.get("object") == "error"]
    deltas = [e for e in events if e.get("choices") and e["choices"][0]["delta"].get("content")]
    assert errors and len(deltas) > 1, events
    assert len(_ids(events)) == 1, _ids(events)


def test_live_stream_receipt_names_the_interaction_for_outcomes(monkeypatch):
    """With live deltas no frame id is the interaction id, so the receipt carries it."""
    import server
    import sonder_runtime.interfaces.http.serve as ts

    # A turn that recorded an interaction (learning is off in _served_turn).
    monkeypatch.setattr(server, "parse_interaction_id", lambda _out: "feedc0de1234")
    passed = []
    send_stream = ts.Handler._send_stream

    def spy(self, content, model, iid=None, **kwargs):
        passed.append(iid)
        return send_stream(self, content, model, iid=iid, **kwargs)

    monkeypatch.setattr(ts.Handler, "_send_stream", spy)
    _first, _total, events, _text = _served_turn(monkeypatch, FakeInference(token_delay=0.0))
    assert passed == ["feedc0de1234"], passed
    (completion_id,) = _ids(events)
    assert completion_id != "chatcmpl-%s" % passed[0]
    receipts = [e["sonder_receipt"] for e in events if "sonder_receipt" in e]
    assert receipts and receipts[-1]["interaction_id"] == passed[0], receipts
