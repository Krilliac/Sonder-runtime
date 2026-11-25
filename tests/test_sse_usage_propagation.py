"""Tests for SSE upstream usage propagation and client-disconnect/cancel hardening."""
from __future__ import annotations

import io
import json
import socket
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference import sse_stream
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
    _error_document_fields,
)
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import Cancelled, DependencyUnavailable
from sonder_runtime.interfaces.http import live_stream
from sonder_runtime.interfaces.http.facades import chat_helpers
from tests.test_sonder_inference_streaming import TOKENS, FakeInference, serving

pytestmark = pytest.mark.unit


def _sse_bytes(frames: list[object]) -> io.BytesIO:
    parts: list[str] = []
    for frame in frames:
        if isinstance(frame, str):
            parts.append(frame if frame.startswith(":") else f"data: {frame}\n\n")
        else:
            parts.append(f"data: {json.dumps(frame)}\n\n")
    return io.BytesIO("".join(parts).encode("utf-8"))


def _parse_sse(raw: bytes) -> list[dict]:
    return [
        json.loads(line[6:])
        for line in raw.decode("utf-8").splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]


class _FakeHandler:
    def __init__(self, *, client_gone: bool = False, wfile=None) -> None:
        self.wfile = wfile if wfile is not None else io.BytesIO()
        self._stream_model = "qwen3-80b"
        self.close_connection = False
        self._early_stream = SimpleNamespace(
            lock=threading.Lock(),
            client_gone=client_gone,
            completion_id=None,
        )


@contextmanager
def _scripted_upstream(events: list[object], *, json_body: dict | None = None, delay: float = 0.0):
    """Minimal loopback Sonder Inference stand-in emitting exact SSE frames."""
    state = {"requests": [], "disconnected": threading.Event(), "finished": threading.Event()}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, status: int, doc: dict) -> None:
            body = json.dumps(doc).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            self._json(200, {
                "status": "ready", "api_version": 1, "version": "0.2.0", "synthetic": False,
                "backends": [{"name": "llamaserver", "available": True, "capabilities": ["streaming"]}],
                "models": [{"id": "qwen3-80b", "backend": "llamaserver", "default": True}],
            })

        def do_POST(self) -> None:
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append(payload)
            if json_body is not None:
                self._json(200, json_body)
                state["finished"].set()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True
            try:
                for item in events:
                    if delay:
                        time.sleep(delay)
                    if isinstance(item, bytes):
                        self.wfile.write(item)
                    elif isinstance(item, str):
                        line = item if item.endswith("\n\n") else f"data: {item}\n\n"
                        self.wfile.write(line.encode("utf-8"))
                    else:
                        self.wfile.write(f"data: {json.dumps(item)}\n\n".encode("utf-8"))
                    self.wfile.flush()
                state["finished"].set()
            except OSError:
                state["disconnected"].set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _served_stream_request(
    monkeypatch,
    upstream_url: str,
    *,
    include_usage: bool = True,
    chat_tools: bool = False,
    hold_code: bool = False,
) -> tuple[list[dict], str]:
    """POST streamed `/v1/chat/completions` through real `serve.Handler`."""
    import server
    import sonder_runtime.bootstrap.chat_tools_wiring as ctw
    import sonder_runtime.interfaces.http.serve as ts

    for name in (
        "SONDER_FAST_PROVIDER", "SONDER_GENERAL_PROVIDER", "SONDER_CODE_PROVIDER",
        "SONDER_REASONING_PROVIDER", "SONDER_VISION_PROVIDER",
        "SONDER_INFERENCE_FALLBACK", "SONDER_INFERENCE_READY_FILE",
        "SONDER_INFERENCE_TIER_MODELS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder_inference")
    monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", upstream_url)
    monkeypatch.setenv("SONDER_CHAT_TOOLS", "1" if chat_tools else "0")
    monkeypatch.setenv("SONDER_CODE_GATE", "1" if hold_code else "0")
    if chat_tools:
        monkeypatch.setattr(ctw, "_model_supports_tools", lambda _srv, _model: True)
    monkeypatch.setattr(server, "_APP_GRAPH", None)
    gateway = SonderInferenceGateway()
    monkeypatch.setattr(server, "_application", lambda: SimpleNamespace(model_gateway=gateway))
    monkeypatch.setattr(server, "_should_learn", lambda tier, learn: False)
    monkeypatch.setattr(server, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "STREAM_HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(ts._legacy_runtime(), "chat_web_response", lambda *a, **k: None)

    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        payload: dict[str, object] = {
            "model": "general",
            "stream": True,
            "messages": [{"role": "user", "content": "hello"}],
        }
        if include_usage:
            payload["stream_options"] = {"include_usage": True}
        body = json.dumps(payload).encode("utf-8")
        request = (
            b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
            b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n" % len(body)
        ) + body
        with socket.create_connection(httpd.server_address, timeout=20) as sock:
            sock.sendall(request)
            buf = b""
            while True:
                piece = sock.recv(65536)
                if not piece:
                    break
                buf += piece
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    raw_text = buf.decode("utf-8")
    body_text = raw_text.split("\r\n\r\n", 1)[1]
    events = [
        json.loads(line[6:])
        for line in body_text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    return events, body_text


# -- End-to-end served HTTP usage propagation (regression for live 0/0/0 bug) --


def test_served_tool_turn_propagates_real_upstream_usage_instead_of_zeros(monkeypatch):
    """Live bug: ChatToolTurn bypassed _make_generate, causing usage 0/0/0."""
    fake = FakeInference(token_delay=0.0)
    with serving(fake) as url:
        events, body_text = _served_stream_request(
            monkeypatch, url, include_usage=True, chat_tools=True,
        )

    assert body_text.rstrip().endswith("data: [DONE]")
    usage_events = [e for e in events if "usage" in e]
    assert len(usage_events) == 1
    assert usage_events[0]["choices"] == []
    assert usage_events[0]["usage"]["prompt_tokens"] == 12
    assert usage_events[0]["usage"]["completion_tokens"] == len(TOKENS)
    assert usage_events[0]["usage"]["total_tokens"] == 12 + len(TOKENS)
    assert usage_events[0]["usage"]["prompt_tokens_details"] == {"cached_tokens": 8}

    terminal = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert len(terminal) == 1
    receipt_usage = terminal[0]["sonder_receipt"]["usage"]
    assert receipt_usage["prompt_tokens"] == 12
    assert receipt_usage["completion_tokens"] == len(TOKENS)
    assert receipt_usage["total_tokens"] == 12 + len(TOKENS)
    assert terminal[0]["sonder_activity"]["tokens_in"] == 12
    assert terminal[0]["sonder_activity"]["tokens_out"] == len(TOKENS)
    assert len({e["id"] for e in events}) == 1


def test_served_single_terminal_chunk_sonder_inference_wire_format(monkeypatch):
    """Matches sonder-inference chat_handler.cpp where usage+timings+finish share one chunk."""
    upstream_events = [
        {
            "id": "chatcmpl-node1", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"role": "assistant", "content": ""}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-node1", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": "Hello "}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-node1", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": "Node1!"}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl-node1", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 1482,
                "completion_tokens": 237,
                "total_tokens": 1719,
                "prompt_tokens_details": {"cached_tokens": 1200},
                "sonder": {"timings": {"prompt_n": 1482, "cache_n": 1200, "predicted_n": 237}},
            },
            "timings": {"prompt_n": 1482, "predicted_n": 237, "cache_n": 1200, "ttft_ms": 18.5},
            "sonder": {"api_version": 1, "request_id": "req-n1", "backend": "llamaserver", "synthetic": False},
        },
        "[DONE]",
    ]
    with _scripted_upstream(upstream_events) as (url, _state):
        events, body_text = _served_stream_request(
            monkeypatch, url, include_usage=True, chat_tools=True,
        )

    assert body_text.rstrip().endswith("data: [DONE]")
    usage_events = [e for e in events if "usage" in e]
    assert len(usage_events) == 1
    assert usage_events[0]["usage"] == {
        "prompt_tokens": 1482,
        "completion_tokens": 237,
        "total_tokens": 1719,
        "prompt_tokens_details": {"cached_tokens": 1200},
    }
    terminal = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert terminal[0]["sonder_receipt"]["usage"] == usage_events[0]["usage"]


def test_served_non_tool_turn_does_not_double_count_and_preserves_details(monkeypatch):
    fake = FakeInference(token_delay=0.0)
    with serving(fake) as url:
        events, _ = _served_stream_request(
            monkeypatch, url, include_usage=True, chat_tools=False,
        )

    usage_events = [e for e in events if "usage" in e]
    assert len(usage_events) == 1
    assert usage_events[0]["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": len(TOKENS),
        "total_tokens": 12 + len(TOKENS),
        "prompt_tokens_details": {"cached_tokens": 8},
    }


def test_served_stream_without_include_usage_omits_usage_chunk_but_populates_receipt(monkeypatch):
    fake = FakeInference(token_delay=0.0)
    with serving(fake) as url:
        events, _ = _served_stream_request(
            monkeypatch, url, include_usage=False, chat_tools=True,
        )

    assert [e for e in events if "usage" in e] == []
    terminal = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert len(terminal) == 1
    assert terminal[0]["sonder_receipt"]["usage"]["prompt_tokens"] == 12
    assert terminal[0]["sonder_receipt"]["usage"]["completion_tokens"] == len(TOKENS)
    assert terminal[0]["sonder_receipt"]["usage"]["total_tokens"] == 12 + len(TOKENS)


# -- sse_stream usage aggregation & non-clobbering resilience ------------------


def test_sse_usage_not_clobbered_by_null_empty_or_placeholder_zero_chunks():
    handler = _FakeHandler()
    live = live_stream.arm(handler, chat_helpers.format_sse_chunk, hold_code=False)
    assert live is not None

    stream = _sse_bytes([
        {
            "id": "c1", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": "Hi"}, "finish_reason": None}],
            "usage": None,
            "timings": None,
        },
        {
            "id": "c1", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": "!"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 250,
                "completion_tokens": 42,
                "total_tokens": 292,
                "prompt_tokens_details": {"cached_tokens": 100},
            },
            "timings": {"prompt_n": 250, "predicted_n": 42, "cache_n": 100},
            "sonder": {"api_version": 1},
        },
        # Trailing chunks with null, empty dict, and zero placeholders must NOT wipe counts.
        {"id": "c1", "object": "chat.completion.chunk", "choices": [], "usage": None, "timings": None, "sonder": None},
        {"id": "c1", "object": "chat.completion.chunk", "choices": [], "usage": {}},
        {
            "id": "c1", "object": "chat.completion.chunk", "choices": [],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
        "[DONE]",
    ])

    doc = sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    assert doc["usage"]["prompt_tokens"] == 250
    assert doc["usage"]["completion_tokens"] == 42
    assert doc["usage"]["total_tokens"] == 292
    assert doc["timings"]["prompt_n"] == 250
    assert doc["sonder"] == {"api_version": 1}
    assert live.upstream_usage == {
        "prompt_tokens": 250,
        "completion_tokens": 42,
        "total_tokens": 292,
        "prompt_tokens_details": {"cached_tokens": 100},
    }


def test_sse_usage_updates_from_placeholder_zeros_and_cumulative_progress():
    live = stream_sink.LiveTurnStream(lambda _text: True)
    stream = _sse_bytes([
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "a"}, "finish_reason": None}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        },
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "b"}, "finish_reason": None}],
            "usage": {"prompt_tokens": 60, "completion_tokens": 5, "total_tokens": 65},
        },
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "c"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 60, "completion_tokens": 14, "total_tokens": 74},
        },
        "[DONE]",
    ])
    doc = sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    assert doc["usage"] == {"prompt_tokens": 60, "completion_tokens": 14, "total_tokens": 74}
    assert live.upstream_usage == {
        "prompt_tokens": 60, "completion_tokens": 14, "total_tokens": 74,
    }


def test_sse_usage_merges_split_chunks_and_input_output_aliases():
    live = stream_sink.LiveTurnStream(lambda _text: True)
    stream = _sse_bytes([
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": None}],
            "usage": {"input_tokens": 88},
        },
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"output_tokens": 19},
        },
        "[DONE]",
    ])
    doc = sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    assert doc["usage"]["prompt_tokens"] == 88
    assert doc["usage"]["completion_tokens"] == 19
    assert doc["usage"]["total_tokens"] == 107
    assert live.upstream_usage == {
        "prompt_tokens": 88, "completion_tokens": 19, "total_tokens": 107,
    }


def test_sse_timings_fallback_without_synthesizing_doc_usage_when_absent():
    live = stream_sink.LiveTurnStream(lambda _text: True)
    stream = _sse_bytes([
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}],
            "timings": {"prompt_n": 95, "predicted_n": 21, "cache_n": 40},
        },
        "[DONE]",
    ])
    doc = sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    # Preserve telemetry.py's invariant: do not synthesize document["usage"] if upstream omitted it.
    assert "usage" not in doc
    assert live.upstream_usage == {
        "prompt_tokens": 95,
        "completion_tokens": 21,
        "total_tokens": 116,
        "prompt_tokens_details": {"cached_tokens": 40},
    }


def test_sse_zeroed_usage_repaired_from_timings_and_nested_sonder_timings():
    live = stream_sink.LiveTurnStream(lambda _text: True)
    stream = _sse_bytes([
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "sonder": {"timings": {"prompt_n": 310, "predicted_n": 55, "cache_n": 200}},
            },
        },
        "[DONE]",
    ])
    doc = sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    assert doc["usage"]["prompt_tokens"] == 310
    assert doc["usage"]["completion_tokens"] == 55
    assert doc["usage"]["total_tokens"] == 365
    assert live.upstream_usage == {
        "prompt_tokens": 310,
        "completion_tokens": 55,
        "total_tokens": 365,
        "prompt_tokens_details": {"cached_tokens": 200},
    }


def test_sse_ignores_invalid_and_boolean_token_counts_and_preserves_malformed_usage_type():
    live = stream_sink.LiveTurnStream(lambda _text: True)
    bad_fields = _sse_bytes([
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": True, "completion_tokens": -5, "total_tokens": "abc"},
        },
        "[DONE]",
    ])
    sse_stream.consume_event_stream(
        bad_fields, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    assert getattr(live, "upstream_usage", None) is None

    malformed = _sse_bytes([
        {
            "id": "c1", "model": "m1",
            "choices": [{"index": 0, "delta": {"content": "x"}, "finish_reason": "stop"}],
            "usage": "not-an-object",
        },
        "[DONE]",
    ])
    doc = sse_stream.consume_event_stream(
        malformed, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    assert doc["usage"] == "not-an-object"


def test_post_streaming_json_fallback_reports_usage_to_live():
    json_doc = {
        "id": "chatcmpl-json", "object": "chat.completion", "model": "qwen3-80b",
        "choices": [{"index": 0, "message": {"role": "assistant", "content": "plain"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 41, "completion_tokens": 9, "total_tokens": 50},
        "sonder": {"api_version": 1},
    }
    handler = _FakeHandler()
    live = live_stream.arm(handler, chat_helpers.format_sse_chunk, hold_code=False)
    assert live is not None
    with _scripted_upstream([], json_body=json_doc) as (url, _state):
        gw = SonderInferenceGateway(SonderInferenceConfig(base_url=url, model="default"), env={})
        with stream_sink.armed(live), stream_sink.claimed_for_call():
            resp = gw.generate(
                ModelRequest(prompt="hi", tier="general"),
                local_owner_context(correlation_id="c-json", source="http", timeout_seconds=10.0),
            )
    assert resp.text == "plain"
    assert live.upstream_usage == {
        "prompt_tokens": 41, "completion_tokens": 9, "total_tokens": 50,
    }


# -- Tool-call, reasoning, held-code, and multi-step usage streams -------------


def test_reasoning_and_tool_call_stream_propagates_usage_and_receipt_flags():
    handler = _FakeHandler()
    live = live_stream.arm(
        handler, chat_helpers.format_sse_chunk, hold_code=False, expose_reasoning=True,
    )
    assert live is not None

    stream = _sse_bytes([
        {
            "id": "c-tool", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"reasoning_content": "Checking weather..."}, "finish_reason": None}],
        },
        {
            "id": "c-tool", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "id": "call_1", "type": "function",
                "function": {"name": "get_weather", "arguments": '{"city":'},
            }]}, "finish_reason": None}],
        },
        {
            "id": "c-tool", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"tool_calls": [{
                "index": 0, "function": {"arguments": '"Austin"}'},
            }]}, "finish_reason": "tool_calls"}],
            "usage": {
                "prompt_tokens": 400,
                "completion_tokens": 75,
                "total_tokens": 475,
                "prompt_tokens_details": {"cached_tokens": 300},
                "completion_tokens_details": {"reasoning_tokens": 40},
            },
        },
        "[DONE]",
    ])
    sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=_error_document_fields,
    )

    ok = live_stream.write_stream_body(
        handler,
        chat_helpers.format_sse_chunk,
        "iid-tool",
        "qwen3-80b",
        "It is 72F in Austin.",
        140,
        {"request_id": "req-tool"},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        {"tokens_in": 0, "tokens_out": 0, "model_calls": 1},
        lock=handler._early_stream.lock,
    )
    assert ok is True

    frames = _parse_sse(handler.wfile.getvalue())
    assert len({f["id"] for f in frames}) == 1
    terminal = [f for f in frames if f.get("choices") and f["choices"][0].get("finish_reason")]
    assert len(terminal) == 1
    assert terminal[0]["sonder_receipt"]["live_stream"]["reasoning_streamed"] is True
    assert terminal[0]["sonder_receipt"]["live_stream"]["tool_calls_streamed"] is True
    expected_usage = {
        "prompt_tokens": 400,
        "completion_tokens": 75,
        "total_tokens": 475,
        "prompt_tokens_details": {"cached_tokens": 300},
        "completion_tokens_details": {"reasoning_tokens": 40},
    }
    assert terminal[0]["sonder_receipt"]["usage"] == expected_usage
    assert terminal[0]["sonder_activity"]["tokens_in"] == 400
    assert terminal[0]["sonder_activity"]["tokens_out"] == 75
    assert frames[-1]["choices"] == []
    assert frames[-1]["usage"] == expected_usage


def test_held_code_fence_stream_still_propagates_real_usage():
    handler = _FakeHandler()
    live = live_stream.arm(handler, chat_helpers.format_sse_chunk, hold_code=True)
    assert live is not None

    stream = _sse_bytes([
        {
            "id": "c-code", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": "```python\nprint(1)\n```"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 110, "completion_tokens": 22, "total_tokens": 132},
        },
        "[DONE]",
    ])
    sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    assert live.forwarded == ""
    assert live.held is True

    ok = live_stream.write_stream_body(
        handler, chat_helpers.format_sse_chunk, "iid-code", "qwen3-80b",
        "```python\nprint(1)\n```", 80, {"request_id": "req-code"},
        {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        None, lock=handler._early_stream.lock,
    )
    assert ok is True
    frames = _parse_sse(handler.wfile.getvalue())
    assert frames[-1]["usage"] == {"prompt_tokens": 110, "completion_tokens": 22, "total_tokens": 132}
    terminal = [f for f in frames if f.get("choices") and f["choices"][0].get("finish_reason")]
    assert terminal[0]["sonder_receipt"]["usage"] == {
        "prompt_tokens": 110, "completion_tokens": 22, "total_tokens": 132,
    }


def test_multi_step_stream_accumulates_upstream_usage_on_same_live_stream():
    handler = _FakeHandler()
    live = live_stream.arm(handler, chat_helpers.format_sse_chunk, hold_code=False)
    assert live is not None

    for prompt_n, comp_n, cached_n in ((100, 20, 40), (150, 35, 110)):
        stream = _sse_bytes([
            {
                "id": "c-multi", "model": "qwen3-80b",
                "choices": [{"index": 0, "delta": {"content": "step "}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": prompt_n,
                    "completion_tokens": comp_n,
                    "total_tokens": prompt_n + comp_n,
                    "prompt_tokens_details": {"cached_tokens": cached_n},
                },
            },
            "[DONE]",
        ])
        sse_stream.consume_event_stream(
            stream, live, limit=1 << 20, error_fields=_error_document_fields,
        )

    assert live.upstream_usage == {
        "prompt_tokens": 250,
        "completion_tokens": 55,
        "total_tokens": 305,
        "prompt_tokens_details": {"cached_tokens": 150},
    }


def test_activity_usage_wins_for_multi_call_turn_and_does_not_mutate_caller_receipt():
    handler = _FakeHandler()
    live = live_stream.arm(handler, chat_helpers.format_sse_chunk, hold_code=False)
    assert live is not None
    live.record_usage({"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60})

    caller_receipt = {"request_id": "req-multi"}
    caller_activity = {"tokens_in": 120, "tokens_out": 40, "model_calls": 2}
    live_stream.write_stream_body(
        handler, chat_helpers.format_sse_chunk, "iid-m", "qwen3-80b",
        "Final", 50, caller_receipt,
        {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160},
        caller_activity, lock=handler._early_stream.lock,
    )
    assert "usage" not in caller_receipt
    frames = _parse_sse(handler.wfile.getvalue())
    assert frames[-1]["usage"] == {"prompt_tokens": 120, "completion_tokens": 40, "total_tokens": 160}


# -- Client-disconnect & cancel cleanup hardening ------------------------------


def test_post_streaming_pre_send_disconnect_never_opens_upstream():
    live = stream_sink.LiveTurnStream(lambda _text: True, client_gone=lambda: True)
    with _scripted_upstream(["[DONE]"]) as (url, state):
        gw = SonderInferenceGateway(SonderInferenceConfig(base_url=url, model="default"), env={})
        with stream_sink.armed(live), stream_sink.claimed_for_call():
            with pytest.raises(Cancelled):
                gw.generate(
                    ModelRequest(prompt="hi", tier="general"),
                    local_owner_context(correlation_id="c-presend", source="http", timeout_seconds=10.0),
                )
    assert state["requests"] == []


def test_consume_event_stream_cancels_immediately_during_keepalive_comments():
    cancelled = {"value": False}
    live = stream_sink.LiveTurnStream(lambda _text: True, client_gone=lambda: cancelled["value"])

    class _KeepAliveStream:
        def __init__(self) -> None:
            self.reads = 0

        def readline(self, _limit: int = -1) -> bytes:
            self.reads += 1
            if self.reads == 2:
                cancelled["value"] = True
            return b": keep-alive\n"

    stream = _KeepAliveStream()
    with pytest.raises(Cancelled):
        sse_stream.consume_event_stream(
            stream, live, limit=1 << 20, error_fields=_error_document_fields,
        )
    assert stream.reads == 2


def test_mid_stream_disconnect_closes_upstream_and_stops_watcher_threads():
    before_names = {t.name for t in threading.enumerate() if t.is_alive()}
    events = [
        {
            "id": "c-disc", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": f"tok{i}"}, "finish_reason": None}],
        }
        for i in range(40)
    ] + ["[DONE]"]

    written = 0

    def fail_on_third(_text: str) -> bool:
        nonlocal written
        written += 1
        return written < 3

    live = stream_sink.LiveTurnStream(fail_on_third)
    with _scripted_upstream(events, delay=0.05) as (url, state):
        gw = SonderInferenceGateway(SonderInferenceConfig(base_url=url, model="default"), env={})
        with stream_sink.armed(live), stream_sink.claimed_for_call():
            with pytest.raises(Cancelled):
                gw.generate(
                    ModelRequest(prompt="hi", tier="general"),
                    local_owner_context(correlation_id="c-disc", source="http", timeout_seconds=10.0),
                )
        assert state["disconnected"].wait(5.0)
        assert not state["finished"].is_set()

    after_names = {t.name for t in threading.enumerate() if t.is_alive()}
    assert "sonder-inference-stream-cancel" not in (after_names - before_names)
    assert "sonder-stream-stall" not in (after_names - before_names)


def test_mid_stream_error_event_cleans_up_sockets_and_threads():
    before_names = {t.name for t in threading.enumerate() if t.is_alive()}
    events = [
        {
            "id": "c-err", "object": "chat.completion.chunk", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": "partial"}, "finish_reason": None}],
        },
        {"error": {"code": "backend_unavailable", "message": "upstream exploded"}},
    ]
    live = stream_sink.LiveTurnStream(lambda _text: True)
    with _scripted_upstream(events) as (url, _state):
        gw = SonderInferenceGateway(
            SonderInferenceConfig(base_url=url, model="default", stream_idle_seconds=5.0),
            env={},
        )
        with stream_sink.armed(live), stream_sink.claimed_for_call():
            with pytest.raises(DependencyUnavailable, match="upstream exploded"):
                gw.generate(
                    ModelRequest(prompt="hi", tier="general"),
                    local_owner_context(correlation_id="c-err", source="http", timeout_seconds=10.0),
                )
    after_names = {t.name for t in threading.enumerate() if t.is_alive()}
    assert "sonder-inference-stream-cancel" not in (after_names - before_names)
    assert "sonder-stream-stall" not in (after_names - before_names)


def test_write_stream_body_short_circuits_when_client_already_gone():
    handler = _FakeHandler(client_gone=True)
    ok = live_stream.write_stream_body(
        handler, chat_helpers.format_sse_chunk, "iid-gone", "qwen3-80b",
        "Answer", 20, {"request_id": "req-gone"},
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        None, lock=handler._early_stream.lock,
    )
    assert ok is False
    assert handler.close_connection is True
    assert handler.wfile.getvalue() == b""


@pytest.mark.parametrize("exc_cls", [BrokenPipeError, ConnectionResetError, ValueError])
def test_write_stream_body_handles_write_disconnect_cleanly_without_done_frame(exc_cls):
    class _BrokenWFile:
        def __init__(self) -> None:
            self.writes: list[bytes] = []

        def write(self, data: bytes) -> int:
            if len(self.writes) >= 1:
                raise exc_cls("client disconnected")
            self.writes.append(data)
            return len(data)

        def flush(self) -> None:
            pass

    wfile = _BrokenWFile()
    handler = _FakeHandler(wfile=wfile)
    ok = live_stream.write_stream_body(
        handler, chat_helpers.format_sse_chunk, "iid-bp", "qwen3-80b",
        "Hello", 15, {"request_id": "req-bp"},
        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        None, lock=handler._early_stream.lock,
    )
    assert ok is False
    assert handler._early_stream.client_gone is True
    assert handler.close_connection is True
    assert all(b"[DONE]" not in chunk for chunk in wfile.writes)


def test_write_stream_body_marks_disconnect_when_final_flush_fails():
    class _FlushFailWFile(io.BytesIO):
        def flush(self) -> None:
            raise BrokenPipeError("peer closed on flush")

    handler = _FakeHandler(wfile=_FlushFailWFile())
    ok = live_stream.write_stream_body(
        handler, chat_helpers.format_sse_chunk, "iid-fl", "qwen3-80b",
        "Hello", 15, {}, None, None, lock=handler._early_stream.lock,
    )
    assert ok is False
    assert handler._early_stream.client_gone is True
    assert handler.close_connection is True


def test_write_stream_body_still_propagates_non_socket_chunk_errors():
    handler = _FakeHandler()

    def bad_chunk(*_args, **_kwargs):
        raise RuntimeError("serialization bug")

    with pytest.raises(RuntimeError, match="serialization bug"):
        live_stream.write_stream_body(
            handler, bad_chunk, "iid-err", "qwen3-80b",
            "Hello", 15, {}, None, None, lock=handler._early_stream.lock,
        )


def test_armed_for_synchronizes_disconnect_and_resets_context_on_cancel():
    class _FailFirstWrite:
        def write(self, _data: bytes) -> int:
            raise BrokenPipeError("gone")

        def flush(self) -> None:
            pass

    handler = _FakeHandler(wfile=_FailFirstWrite())
    with pytest.raises(Cancelled):
        with live_stream.armed_for(handler, chat_helpers.format_sse_chunk, hold_code=False) as live:
            assert live is not None
            assert live.emit("hello") is False
            raise Cancelled("client disconnected")

    assert handler._early_stream.client_gone is True
    assert handler.close_connection is True
    assert stream_sink.turn_stream() is None


def test_usage_detail_keys_are_bounded_and_sanitized():
    handler = _FakeHandler()
    live = live_stream.arm(handler, chat_helpers.format_sse_chunk, hold_code=False)
    assert live is not None
    noisy_details = {f"k{i}": i for i in range(40)}
    noisy_details["bad\nkey"] = 5
    noisy_details["x" * 100] = 5
    noisy_details["cached_tokens"] = 9999  # exceeds prompt_tokens=100, must be dropped
    stream = _sse_bytes([
        {
            "id": "c-bound", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": "ok"}, "finish_reason": "stop"}],
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 110,
                "prompt_tokens_details": noisy_details,
            },
        },
        "[DONE]",
    ])
    sse_stream.consume_event_stream(
        stream, live, limit=1 << 20, error_fields=_error_document_fields,
    )
    details = live.upstream_usage["prompt_tokens_details"]
    assert len(details) <= 16
    assert "bad\nkey" not in details
    assert ("x" * 100) not in details
    assert "cached_tokens" not in details


def test_proxy_wrapper_forwards_upstream_usage_to_inner_live_stream():
    handler = _FakeHandler()
    inner = live_stream.arm(handler, chat_helpers.format_sse_chunk, hold_code=False)
    assert inner is not None

    class _Proxy:
        def __init__(self, wrapped) -> None:
            self._live = wrapped

        @property
        def cancelled(self) -> bool:
            return self._live.cancelled

        def emit(self, text: str) -> bool:
            return self._live.emit(text)

    proxy = _Proxy(inner)
    stream = _sse_bytes([
        {
            "id": "c-proxy", "model": "qwen3-80b",
            "choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 77, "completion_tokens": 11, "total_tokens": 88},
        },
        "[DONE]",
    ])
    sse_stream.consume_event_stream(
        stream, proxy, limit=1 << 20, error_fields=_error_document_fields,
    )
    assert inner.upstream_usage == {
        "prompt_tokens": 77, "completion_tokens": 11, "total_tokens": 88,
    }

