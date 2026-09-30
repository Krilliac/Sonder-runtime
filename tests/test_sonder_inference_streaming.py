"""Live token streaming from Sonder Inference through the gateway (G2).

A loopback HTTP server stands in for ``sonder-infer serve``: it answers
``/v1/sonder/health`` and ``/v1/chat/completions`` both as one JSON document
and as ``text/event-stream`` with a configurable delay per token, so these
tests measure what a user would see: the time to the first forwarded token.
"""
from __future__ import annotations

import io
import json
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sonder_runtime.adapters.inference import sse_stream
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    SonderInferenceConfig,
    SonderInferenceGateway,
    SonderInferenceUnreachable,
    _error_document_fields,
)
from sonder_runtime.application.chat import provider_bridge as bridge
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import Cancelled, DependencyUnavailable

TOKENS = ["Hello", ",", " streamed", " world", "!", " One", " more", " line", " of", " text."]


class FakeInference:
    """Scriptable Sonder Inference stand-in with per-token pacing."""

    def __init__(self, *, token_delay=0.05, prefill_delay=0.0, tokens=TOKENS,
                 error_after=None, health_extra=None, json_to_stream=False,
                 status=200, error_code=""):
        self.token_delay = token_delay
        self.prefill_delay = prefill_delay
        self.tokens = list(tokens)
        self.error_after = error_after
        self.health_extra = dict(health_extra or {})
        self.json_to_stream = json_to_stream
        self.status = status
        self.error_code = error_code
        self.requests: list[dict] = []
        self.written = 0
        self.disconnected = threading.Event()
        self.finished = threading.Event()

    def health(self):
        document = {
            "status": "ready", "api_version": 1, "version": "0.1.0", "synthetic": False,
            "backends": [{"name": "llamaserver", "available": True,
                          "capabilities": ["streaming"]}],
            "models": [{"id": "qwen3.8:27b", "backend": "llamaserver", "default": True}],
        }
        document.update(self.health_extra)
        return document

    def final(self):
        return {
            "id": "chatcmpl-r1", "object": "chat.completion.chunk", "model": "qwen3.8:27b",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "timings": {"prompt_n": 12, "predicted_n": len(self.tokens), "total_ms": 90.0,
                        "ttft_ms": 7.5, "cache_n": 8, "draft_n": 6, "draft_n_accepted": 4},
            "sonder": {"api_version": 1, "request_id": "r1", "backend": "llamaserver",
                       "synthetic": False},
        }


@contextmanager
def serving(fake: FakeInference):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, status, document):
            body = json.dumps(document).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self._json(200, fake.health())

        def _event(self, document):
            data = document if isinstance(document, str) else json.dumps(document)
            self.wfile.write(("data: %s\n\n" % data).encode())
            self.wfile.flush()

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            fake.requests.append(payload)
            if fake.status != 200:
                self._json(fake.status, {"error": {"code": fake.error_code, "message": "x"}})
                return
            text = "".join(fake.tokens)
            if not payload.get("stream") or fake.json_to_stream:
                time.sleep(fake.prefill_delay + fake.token_delay * len(fake.tokens))
                final = fake.final()
                self._json(200, {
                    "id": "chatcmpl-r1", "object": "chat.completion", "model": "qwen3.8:27b",
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 12, "completion_tokens": len(fake.tokens),
                              "total_tokens": 12 + len(fake.tokens)},
                    "timings": final["timings"], "sonder": final["sonder"],
                })
                fake.finished.set()
                return
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True
            try:
                time.sleep(fake.prefill_delay)
                self.wfile.write(b": keep-alive\n\n")
                self._event({"id": "chatcmpl-r1", "object": "chat.completion.chunk",
                             "model": "qwen3.8:27b",
                             "choices": [{"index": 0, "delta": {"role": "assistant"},
                                          "finish_reason": None}]})
                for index, token in enumerate(fake.tokens):
                    if fake.error_after is not None and index == fake.error_after:
                        self._event({"error": {"code": "backend_unavailable",
                                               "message": "backend died"}})
                        return
                    time.sleep(fake.token_delay)
                    self._event({"id": "chatcmpl-r1", "object": "chat.completion.chunk",
                                 "model": "qwen3.8:27b",
                                 "choices": [{"index": 0, "delta": {"content": token},
                                              "finish_reason": None}]})
                    fake.written += 1
                self._event(fake.final())
                self._event({"id": "chatcmpl-r1", "object": "chat.completion.chunk",
                             "model": "qwen3.8:27b", "choices": [],
                             "usage": {"prompt_tokens": 12,
                                       "completion_tokens": len(fake.tokens),
                                       "total_tokens": 12 + len(fake.tokens),
                                       "prompt_tokens_details": {"cached_tokens": 8}}})
                self._event("[DONE]")
                fake.finished.set()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                fake.disconnected.set()

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _gateway(base_url, env=None):
    return SonderInferenceGateway(
        SonderInferenceConfig(base_url=base_url, model="default"), env=env or {},
    )


def _ctx(timeout=30.0):
    return local_owner_context(correlation_id="turn-1", source="http", timeout_seconds=timeout)


class Recorder:
    """A client that records when each delta arrived."""

    def __init__(self, *, fail_after=None):
        self.deltas: list[tuple[float, str]] = []
        self.fail_after = fail_after
        self.gone = False

    def write(self, text):
        if self.fail_after is not None and len(self.deltas) >= self.fail_after:
            return False
        self.deltas.append((time.monotonic(), text))
        return True


def _bridged_turn(gateway, recorder, *, provider="sonder_inference", hold=None):
    """Run one bridged chat step with a live stream armed, as serve.py does."""
    live = stream_sink.LiveTurnStream(
        recorder.write, client_gone=lambda: recorder.gone, hold_marker=hold,
    )
    payload = {"model": "ignored", "messages": [{"role": "user", "content": "hi"}],
               "options": {"temperature": 0.2, "num_predict": 64}}
    started = time.monotonic()
    with stream_sink.armed(live), bridge.bind_rung(provider, "general"):
        shaped, response = bridge.generate_via_gateway(
            gateway, payload, tier="general", context=_ctx(),
        )
    return live, shaped, response, started, time.monotonic()


# -- time to first token ------------------------------------------------------


def test_streaming_cuts_time_to_first_token_to_one_token():
    fake = FakeInference(token_delay=0.08)
    with serving(fake) as url:
        gateway = _gateway(url)
        # Before (the non-streaming route): nothing is visible until the
        # whole completion has been generated and returned.
        started = time.monotonic()
        before = gateway.generate(ModelRequest(prompt="hi", tier="general"), _ctx())
        before_ttft = time.monotonic() - started
        recorder = Recorder()
        live, shaped, response, turn_started, turn_done = _bridged_turn(gateway, recorder)
    after_ttft = recorder.deltas[0][0] - turn_started
    total = turn_done - turn_started
    print("TTFT before=%.3fs after=%.3fs (generation %.3fs, %d tokens x %.2fs)"
          % (before_ttft, after_ttft, total, len(TOKENS), fake.token_delay))
    assert before.text == response.text == "".join(TOKENS)
    assert fake.requests[0]["stream"] is False
    assert fake.requests[1]["stream"] is True
    assert fake.requests[1]["stream_options"] == {"include_usage": True}
    # One token's delay (plus the health check), not the whole generation.
    assert after_ttft < before_ttft / 3
    assert after_ttft < 0.5
    assert "".join(text for _at, text in recorder.deltas) == "".join(TOKENS)
    assert live.forwarded == response.text and live.reconcile(response.text) == ("", False)
    # Deltas arrived progressively, not in one burst at the end.
    assert recorder.deltas[-1][0] - recorder.deltas[0][0] > 0.5 * fake.token_delay * len(TOKENS)


def test_streamed_response_keeps_usage_telemetry_and_served_model():
    fake = FakeInference(token_delay=0.0)
    with serving(fake) as url:
        _live, shaped, response, _s, _d = _bridged_turn(_gateway(url), Recorder())
    assert response.model == "qwen3.8:27b"
    assert (response.tokens_in, response.tokens_out) == (12, len(TOKENS))
    telemetry = response.telemetry
    assert telemetry.ttft_ms == 7.5
    assert (telemetry.prompt_tokens, telemetry.prompt_cached_tokens,
            telemetry.prompt_uncached_tokens) == (12, 8, 4)
    assert (telemetry.draft_tokens, telemetry.draft_accepted_tokens) == (6, 4)
    assert shaped["prompt_eval_cached_count"] == 8
    assert shaped["prompt_eval_count"] == 12 and shaped["eval_count"] == len(TOKENS)


# -- only the first bridged generation of a turn streams -----------------------


def test_second_generation_in_the_same_turn_does_not_stream():
    fake = FakeInference(token_delay=0.0)
    with serving(fake) as url:
        gateway = _gateway(url)
        recorder = Recorder()
        live = stream_sink.LiveTurnStream(recorder.write)
        payload = {"messages": [{"role": "user", "content": "hi"}]}
        with stream_sink.armed(live), bridge.bind_rung("sonder_inference", "general"):
            bridge.generate_via_gateway(gateway, payload, tier="general", context=_ctx())
            bridge.generate_via_gateway(gateway, payload, tier="general", context=_ctx())
    assert [request["stream"] for request in fake.requests] == [True, False]
    assert "".join(text for _at, text in recorder.deltas) == "".join(TOKENS)


def test_other_providers_never_claim_the_stream():
    class Gateway:
        def generate(self, request, context):
            assert stream_sink.call_stream() is None
            from sonder_runtime.application.ports.model_gateway import ModelResponse
            return ModelResponse(text="answer", model="m", tier="general")

    live = stream_sink.LiveTurnStream(lambda text: True)
    with stream_sink.armed(live), bridge.bind_rung("openai_compatible", "general"):
        bridge.generate_via_gateway(
            Gateway(), {"messages": [{"role": "user", "content": "hi"}]},
            tier="general", context=_ctx(),
        )
    assert live.claimed is False


def test_unarmed_turns_send_the_historical_non_streaming_request():
    fake = FakeInference(token_delay=0.0)
    with serving(fake) as url:
        with bridge.bind_rung("sonder_inference", "general"):
            bridge.generate_via_gateway(
                _gateway(url), {"messages": [{"role": "user", "content": "hi"}]},
                tier="general", context=_ctx(),
            )
    assert fake.requests[0]["stream"] is False
    assert "stream_options" not in fake.requests[0]


# -- cancellation --------------------------------------------------------------


def test_client_disconnect_mid_stream_cancels_and_closes_upstream():
    fake = FakeInference(token_delay=0.1)
    with serving(fake) as url:
        with pytest.raises(Cancelled):
            _bridged_turn(_gateway(url), Recorder(fail_after=2))
        assert fake.disconnected.wait(5)
    assert fake.written < len(TOKENS)
    assert not fake.finished.is_set()


def test_client_gone_during_prefill_cancels_before_any_token():
    fake = FakeInference(prefill_delay=3.0, token_delay=0.0)
    recorder = Recorder()
    with serving(fake) as url:
        timer = threading.Timer(0.3, lambda: setattr(recorder, "gone", True))
        timer.start()
        started = time.monotonic()
        with pytest.raises(Cancelled):
            _bridged_turn(_gateway(url), recorder)
        elapsed = time.monotonic() - started
    assert elapsed < 2.0
    assert recorder.deltas == []


# -- failures keep the non-streaming classification ------------------------------


def test_error_event_after_tokens_is_a_dependency_failure_not_unreachable():
    fake = FakeInference(token_delay=0.0, error_after=3)
    recorder = Recorder()
    with serving(fake) as url:
        with pytest.raises(DependencyUnavailable) as info:
            _bridged_turn(_gateway(url), recorder)
    assert not isinstance(info.value, SonderInferenceUnreachable)
    assert "backend died" in str(info.value)
    assert len(recorder.deltas) == 3


def test_not_ready_before_the_stream_is_unreachable_as_before():
    fake = FakeInference(status=503, error_code="not_ready")
    with serving(fake) as url:
        with pytest.raises(SonderInferenceUnreachable):
            _bridged_turn(_gateway(url), Recorder())


def test_a_json_answer_to_a_stream_request_is_accepted():
    fake = FakeInference(token_delay=0.0, json_to_stream=True)
    recorder = Recorder()
    with serving(fake) as url:
        live, _shaped, response, _s, _d = _bridged_turn(_gateway(url), recorder)
    assert response.text == "".join(TOKENS)
    assert recorder.deltas == [] and live.reconcile(response.text) == (response.text, False)


# -- event-stream parser --------------------------------------------------------


def _parse(body: bytes, live=None, limit=1_000_000):
    return sse_stream.consume_event_stream(
        io.BytesIO(body), live, limit=limit, error_fields=_error_document_fields,
    )


def test_parser_joins_multiline_data_and_ignores_comments_and_other_fields():
    body = (b": hi\n\nevent: x\nid: 1\ndata: {\"model\": \"m\", \"choices\": \n"
            b"data: [{\"delta\": {\"content\": \"a\"}}]}\n\n"
            b"data: {\"choices\": [{\"delta\": {\"content\": \"b\"}, \"finish_reason\": \"length\"}]}\r\n\r\n"
            b"data: [DONE]\n\n")
    document = _parse(body)
    assert document["model"] == "m"
    assert document["choices"][0]["message"]["content"] == "ab"
    assert document["choices"][0]["finish_reason"] == "length"


@pytest.mark.parametrize("body", [
    b"data: {\"choices\": [{\"delta\": {\"content\": \"a\"}}]}\n\n",  # no [DONE]
    b"data: not json\n\n",
    b"data: []\n\n",
])
def test_parser_refuses_truncated_or_malformed_streams(body):
    with pytest.raises(DependencyUnavailable):
        _parse(body)


def test_parser_bounds_the_body():
    body = b"data: {\"choices\": [{\"delta\": {\"content\": \"%s\"}}]}\n\n" % (b"x" * 200)
    with pytest.raises(DependencyUnavailable, match="exceeds"):
        _parse(body * 10, limit=500)


# -- end to end: served HTTP turn -> bridge -> gateway -> Inference -------------


def _served_turn(monkeypatch, fake, *, prompt="hello"):
    """POST one streamed chat turn to the real serve handler; return timings and events."""
    import socket
    from types import SimpleNamespace

    import server
    import sonder_runtime.interfaces.http.serve as ts

    with serving(fake) as url:
        for name in ("SONDER_FAST_PROVIDER", "SONDER_GENERAL_PROVIDER", "SONDER_CODE_PROVIDER",
                     "SONDER_REASONING_PROVIDER", "SONDER_VISION_PROVIDER",
                     "SONDER_INFERENCE_FALLBACK", "SONDER_INFERENCE_READY_FILE",
                     "SONDER_INFERENCE_TIER_MODELS"):
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("SONDER_MODEL_BACKEND", "sonder_inference")
        monkeypatch.setenv("SONDER_INFERENCE_BASE_URL", url)
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
        monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
        httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            body = json.dumps({"model": "general", "stream": True,
                               "messages": [{"role": "user", "content": prompt}]}).encode()
            request = (b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1\r\n"
                       b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
                       % len(body)) + body
            arrivals = []
            with socket.create_connection(httpd.server_address, timeout=30) as sock:
                started = time.monotonic()
                sock.sendall(request)
                buffer = b""
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    buffer += chunk
                    arrivals.append((time.monotonic() - started, chunk))
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=5)
    text = buffer.decode()
    events = []
    first_content_at = None
    seen = b""
    for at, chunk in arrivals:
        seen += chunk
        if first_content_at is None and b'"content": "' in seen.split(b"\r\n\r\n", 1)[-1]:
            first_content_at = at
    for line in text.split("\r\n\r\n", 1)[1].splitlines():
        if line.startswith("data: ") and line != "data: [DONE]":
            events.append(json.loads(line[6:]))
    total = arrivals[-1][0]
    return first_content_at, total, events, text


def _content(events):
    return "".join(
        (event["choices"][0]["delta"].get("content") or "")
        for event in events if event.get("choices")
    )


def test_served_turn_streams_tokens_as_they_are_generated(monkeypatch):
    # Before this change the served route sent the finished answer as one
    # chunk; STREAMING_PROVIDERS empty reproduces that path exactly.
    with monkeypatch.context() as patch:
        patch.setattr(bridge, "STREAMING_PROVIDERS", frozenset())
        before_first, before_total, before_events, _ = _served_turn(
            patch, FakeInference(token_delay=0.1),
        )
    fake = FakeInference(token_delay=0.1)
    after_first, after_total, events, text = _served_turn(monkeypatch, fake)
    print("served TTFT before=%.3fs (turn %.3fs) after=%.3fs (turn %.3fs)"
          % (before_first, before_total, after_first, after_total))
    assert _content(before_events) == _content(events) == "".join(TOKENS)
    content_events = [e for e in before_events if e.get("choices") and e["choices"][0]["delta"].get("content")]
    assert len(content_events) == 1  # historical: one chunk with the whole answer
    assert fake.requests[-1]["stream"] is True
    # The first token reaches the client long before the turn completes.
    assert after_first < after_total - 0.5
    assert after_first < before_first - 0.5
    final = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert final[0]["sonder_receipt"]["live_stream"]["revised"] is False
    assert "ttft_ms" in final[0]["sonder_receipt"]["live_stream"]
    assert text.rstrip().endswith("data: [DONE]")


def test_served_turn_holds_back_code_until_the_gate_has_run(monkeypatch):
    tokens = ["Run this:\n", "```python\n", "import os\n", "print(os.sep)\n", "```\n"]
    fake = FakeInference(token_delay=0.05, tokens=tokens)
    monkeypatch.setenv("SONDER_CODE_GATE", "1")
    import server
    monkeypatch.setattr(server, "_gate_answer_code",
                        lambda response, **kwargs: (response, None, False, {}))
    first, total, events, _text = _served_turn(monkeypatch, fake)
    deltas = [e["choices"][0]["delta"]["content"] for e in events
              if e.get("choices") and e["choices"][0]["delta"].get("content")]
    assert deltas[0] == "Run this:\n"
    assert "```" not in deltas[0]
    # The turn's answer (the pipeline trims trailing whitespace) arrives whole.
    assert _content(events) == "".join(tokens).rstrip()


def test_served_turn_announces_a_revision_after_streaming(monkeypatch):
    fake = FakeInference(token_delay=0.0)
    import server
    monkeypatch.setattr(server, "_gate_answer_code",
                        lambda response, **kwargs: ("Repaired answer.", True, False, {}))
    _first, _total, events, _text = _served_turn(monkeypatch, fake)
    content = _content(events)
    assert content.startswith("".join(TOKENS))
    assert stream_sink.REVISION_NOTICE in content and content.endswith("Repaired answer.")
    final = [e for e in events if e.get("choices") and e["choices"][0].get("finish_reason")]
    assert final[0]["sonder_receipt"]["live_stream"]["revised"] is True
