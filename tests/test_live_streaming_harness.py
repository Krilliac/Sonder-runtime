"""Tests for live reasoning, tool-call, and REPL streaming across Sonder's harness."""
from __future__ import annotations

import io
import json
import threading
import types

from sonder_runtime.adapters.inference import sse_stream
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.interfaces.http import live_stream
from sonder_runtime.interfaces.http.facades import chat_helpers
from sonder_runtime.interfaces.repl import repl


def _sse_bytes(frames: list[str]) -> io.BytesIO:
    return io.BytesIO("".join(f"data: {f}\n\n" for f in frames).encode("utf-8"))


def test_sse_stream_forwards_reasoning_tool_calls_and_content_live():
    content_seen: list[str] = []
    reasoning_seen: list[str] = []
    tools_seen: list[dict] = []

    live = stream_sink.LiveTurnStream(
        lambda text: (content_seen.append(text), True)[1],
        write_reasoning=lambda text: (reasoning_seen.append(text), True)[1],
        write_tool_call=lambda delta: (tools_seen.append(delta), True)[1],
    )
    stream = _sse_bytes([
        json.dumps({
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b",
            "choices": [{"index": 0, "delta": {"reasoning_content": "step 1. "}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b",
            "choices": [{"index": 0, "delta": {"reasoning": "step 2."}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0,
                "id": "call_1",
                "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":'},
            }]}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0,
                "function": {"arguments": '"Tokyo"}'},
            }]}, "finish_reason": None}],
        }),
        json.dumps({
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "bonsai-9b",
            "choices": [{"index": 0, "delta": {"content": "Sunny"}, "finish_reason": "stop"}],
        }),
        "[DONE]",
    ])

    doc = sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=lambda d: ("", ""),
    )
    assert reasoning_seen == ["step 1. ", "step 2."]
    assert len(tools_seen) == 2
    assert content_seen == ["Sunny"]
    assert live.reasoning_forwarded == "step 1. step 2."
    assert live.tool_call_deltas_forwarded == 2
    assert live.any_forwarded is True

    msg = doc["choices"][0]["message"]
    assert msg["content"] == "Sunny"
    assert msg["reasoning_content"] == "step 1. step 2."
    assert msg["tool_calls"] == [{
        "id": "call_1",
        "type": "function",
        "function": {"name": "get_weather", "arguments": '{"city":"Tokyo"}'},
    }]


def test_http_live_stream_respects_expose_reasoning_gate():
    class _FakeHandler:
        def __init__(self) -> None:
            self.wfile = io.BytesIO()
            self._stream_model = "sonder"
            self._early_stream = types.SimpleNamespace(
                lock=threading.Lock(), client_gone=False, completion_id=None,
            )

    # 1. expose_reasoning=False MUST NOT emit reasoning_content deltas over SSE.
    h_hidden = _FakeHandler()
    s_hidden = live_stream.arm(
        h_hidden, chat_helpers.format_sse_chunk, hold_code=False, expose_reasoning=False,
    )
    assert s_hidden is not None
    assert s_hidden.emit_reasoning("secret chain of thought") is True
    assert s_hidden.reasoning_forwarded == ""
    assert s_hidden.reasoning_generated == "secret chain of thought"
    assert h_hidden.wfile.getvalue() == b""

    # 2. expose_reasoning=True streams reasoning_content + tool_calls + content under one id.
    h_vis = _FakeHandler()
    s_vis = live_stream.arm(
        h_vis, chat_helpers.format_sse_chunk, hold_code=False, expose_reasoning=True,
    )
    assert s_vis is not None
    assert s_vis.emit_reasoning("plan A") is True
    assert s_vis.emit_tool_call_delta({
        "index": 0, "id": "call_x", "type": "function",
        "function": {"name": "search", "arguments": "{}"},
    }) is True
    assert s_vis.emit("Answer") is True
    assert live_stream.response_id(h_vis, "fallback-id") == h_vis._early_stream.completion_id

    raw_frames = [
        json.loads(line[6:])
        for line in h_vis.wfile.getvalue().decode("utf-8").splitlines()
        if line.startswith("data: ")
    ]
    assert len(raw_frames) == 3
    assert raw_frames[0]["choices"][0]["delta"] == {"role": "assistant", "reasoning_content": "plan A"}
    assert raw_frames[1]["choices"][0]["delta"]["tool_calls"][0]["function"]["name"] == "search"
    assert raw_frames[2]["choices"][0]["delta"] == {"content": "Answer"}
    assert len({f["id"] for f in raw_frames}) == 1


def test_http_live_stream_reasoning_only_reuses_id_and_omits_duplicate_role():
    class _FakeHandler:
        def __init__(self) -> None:
            self.wfile = io.BytesIO()
            self._stream_model = "sonder"
            self._early_stream = types.SimpleNamespace(
                lock=threading.Lock(), client_gone=False, completion_id=None,
            )

    handler = _FakeHandler()
    live = live_stream.arm(
        handler, chat_helpers.format_sse_chunk, hold_code=False, expose_reasoning=True,
    )
    assert live is not None
    assert live.emit_reasoning("thinking...") is True
    # Content was held/not streamed live; write_stream_body sends final content cleanly.
    live_stream.write_stream_body(
        handler, chat_helpers.format_sse_chunk, "iid-final", "sonder",
        "Final answer", 12, {}, None, None, lock=handler._early_stream.lock,
    )
    frames = [
        json.loads(line[6:])
        for line in handler.wfile.getvalue().decode("utf-8").splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert frames[0]["choices"][0]["delta"] == {"role": "assistant", "reasoning_content": "thinking..."}
    assert frames[1]["choices"][0]["delta"] == {"content": "Final answer"}
    assert frames[0]["id"] == frames[1]["id"] == frames[2]["id"]
    assert frames[2]["sonder_receipt"]["live_stream"]["reasoning_streamed"] is True


def test_repl_live_stream_streams_and_reconciles_without_duplication(monkeypatch, capsys):
    monkeypatch.setattr(repl, "_console_has_operator", lambda: True)
    monkeypatch.setattr(repl, "_stdout_is_interactive", lambda: True)
    monkeypatch.setattr(repl, "_cols", lambda: 80)
    monkeypatch.delenv("SONDER_REPL_NDJSON", raising=False)

    stopped = []

    class _FakeIndicator:
        def stop(self) -> None:
            stopped.append(True)

    live = repl._make_repl_live_stream(_FakeIndicator(), expose_reasoning=True)
    assert live is not None
    assert live.emit_reasoning("checking ") is True
    assert live.emit("Hello ") is True
    assert stopped == [True]

    repl._print_chat_result(
        "Hello world", 0.0, indicator=_FakeIndicator(), live_stream=live,
    )
    out = capsys.readouterr().out
    assert "thinking:" in out
    assert "checking " in out
    assert "Hello world" in out
    assert out.count("Hello ") == 1
