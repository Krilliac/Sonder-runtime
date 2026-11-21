"""Streaming idle and first-content timeouts (slow models, fail-fast stalls).

A threading ``http.server`` plays the upstream: it answers ``/v1/sonder/health``
and streams a scripted sequence of SSE steps.  Each test sets a small stall
limit and checks which limit fired (and the elapsed numbers in the text), or
that a slow-but-alive stream completes under the same limits.
"""
from __future__ import annotations

import contextvars
import json
import re
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from sonder_runtime.adapters.inference import sse_stream
from sonder_runtime.adapters.inference.openai_compat_gateway import (
    OpenAICompatibleConfig,
    OpenAICompatibleGateway,
)
from sonder_runtime.adapters.inference.sonder_inference_gateway import (
    ENV_STREAM_FIRST_CONTENT,
    ENV_STREAM_IDLE,
    SonderInferenceConfig,
    SonderInferenceGateway,
    config_from_env,
)
from sonder_runtime.application.chat import stream_sink
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import DeadlineExceeded, InvalidInput

HEALTH = {
    "status": "ready", "api_version": 1, "version": "0.1.0", "synthetic": False,
    "backends": [{"name": "llamaserver", "available": True, "capabilities": ["streaming"]}],
    "models": [{"id": "m1", "backend": "llamaserver", "default": True}],
}


def chunk(delta, finish=None):
    return {"id": "c1", "object": "chat.completion.chunk", "model": "m1",
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}


FINAL = [
    ("event", chunk({}, "stop")),
    ("event", {"id": "c1", "object": "chat.completion.chunk", "model": "m1", "choices": [],
               "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}}),
    ("raw", b"data: [DONE]\n\n"),
]


class Upstream:
    """Scripted SSE upstream.  ``script`` is a list of steps:

    ``("sleep", s)``, ``("raw", bytes)``, ``("event", dict)`` or
    ``("forever_keepalive", interval)`` (keep-alive comments until the client
    leaves or the test ends).
    """

    def __init__(self, script):
        self.script = script
        self.requests: list[dict] = []
        self.stop = threading.Event()


@contextmanager
def serving(upstream: Upstream):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            body = json.dumps(HEALTH).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            upstream.requests.append(payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.close_connection = True
            try:
                for kind, arg in upstream.script:
                    if kind == "sleep":
                        upstream.stop.wait(arg)
                    elif kind == "raw":
                        self.wfile.write(arg)
                        self.wfile.flush()
                    elif kind == "event":
                        self.wfile.write(("data: %s\n\n" % json.dumps(arg)).encode())
                        self.wfile.flush()
                    elif kind == "forever_keepalive":
                        while not upstream.stop.wait(arg):
                            self.wfile.write(b": keep-alive\n\n")
                            self.wfile.write(b'data: ' + json.dumps(chunk({})).encode() + b"\n\n")
                            self.wfile.flush()
            except OSError:
                pass

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.daemon_threads = True
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        upstream.stop.set()
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def content_script(tokens, delay):
    steps: list = [("event", chunk({"role": "assistant"}))]
    for token in tokens:
        steps.append(("sleep", delay))
        steps.append(("event", chunk({"content": token})))
    return steps + FINAL


def _ctx():
    return local_owner_context(correlation_id="turn-1", source="http", timeout_seconds=60.0)


class Sink:
    def __init__(self):
        self.text: list[str] = []

    def write(self, piece):
        self.text.append(piece)
        return True


def _sonder(url, **limits):
    return SonderInferenceGateway(
        SonderInferenceConfig(base_url=url, model="default", **limits), env={},
    )


def _compat(url, **limits):
    return OpenAICompatibleGateway(
        OpenAICompatibleConfig(base_url=url, model="m1", **limits),
    )


def _stream(gateway):
    sink = Sink()
    live = stream_sink.LiveTurnStream(sink.write)
    started = time.monotonic()
    try:
        with stream_sink.armed(live), stream_sink.claimed_for_call():
            response = gateway.generate(ModelRequest(prompt="hi", tier="general"), _ctx())
    finally:
        elapsed = time.monotonic() - started
    return response, sink, elapsed


GATEWAYS = [pytest.param(_sonder, id="sonder_inference"), pytest.param(_compat, id="openai_compatible")]


@pytest.mark.parametrize("make", GATEWAYS)
def test_keepalives_then_slow_content_does_not_time_out(make):
    # 1.0 s of keep-alives and empty deltas, then content 0.25 s apart: the
    # stream outlives the idle limit in total (2.75 s against 2.0 s) but is
    # never silent for more than 0.25 s, and the first content (1.25 s) lands
    # well inside the first-content limit.
    script = [("event", chunk({"role": "assistant"}))]
    for _ in range(4):
        script += [("raw", b": keep-alive\n\n"), ("event", chunk({})), ("sleep", 0.25)]
    script += content_script(list("abcdefg"), 0.25)
    upstream = Upstream(script)
    with serving(upstream) as url:
        response, sink, elapsed = _stream(make(url, stream_idle_seconds=2.0,
                                               stream_first_content_seconds=10.0))
    assert response.text == "abcdefg"
    assert "".join(sink.text) == "abcdefg"
    assert upstream.requests[0]["stream"] is True
    assert elapsed > 2.0


@pytest.mark.parametrize("make", GATEWAYS)
def test_a_mid_stream_stall_fires_the_idle_timeout(make):
    upstream = Upstream(
        [("event", chunk({"content": "partial"})), ("sleep", 30.0)] + FINAL
    )
    started = time.monotonic()
    with serving(upstream) as url:
        with pytest.raises(DeadlineExceeded) as raised:
            _stream(make(url, stream_idle_seconds=2.0))
    elapsed = time.monotonic() - started
    message = str(raised.value)
    assert "idle timeout" in message, message
    assert "limit 2.0s" in message, message
    assert "first content already received" in message, message
    assert "first-content" not in message, message
    found = re.search(r"no bytes for (\d+\.\d)s", message)
    assert found is not None, message
    assert 2.0 <= float(found.group(1)) < 12.0, message
    assert elapsed < 15.0, message


@pytest.mark.parametrize("make", GATEWAYS)
def test_keepalives_without_content_fire_the_first_content_timeout(make):
    # Bytes keep arriving, so idle never fires; no content ever does.
    upstream = Upstream([("forever_keepalive", 0.1)])
    started = time.monotonic()
    with serving(upstream) as url:
        with pytest.raises(DeadlineExceeded) as raised:
            _stream(make(url, stream_idle_seconds=8.0, stream_first_content_seconds=2.0))
    elapsed = time.monotonic() - started
    message = str(raised.value)
    assert "first-content timeout" in message, message
    assert "limit 2.0s" in message, message
    assert "keep-alives only" in message, message
    assert "idle timeout" not in message, message
    found = re.search(r"after (\d+\.\d)s", message)
    assert found is not None, message
    assert 2.0 <= float(found.group(1)) < 12.0, message
    assert elapsed < 15.0, message


def test_the_total_budget_stays_the_outer_cap_and_is_named():
    upstream = Upstream([("event", chunk({"content": "x"})), ("forever_keepalive", 0.05)])
    with serving(upstream) as url:
        gateway = _sonder(url, timeout_seconds=1.0, stream_idle_seconds=30.0,
                          stream_first_content_seconds=30.0)
        # Warm the health cache: the 1.0 s call budget also caps the probe,
        # which a loaded machine can miss before any stream starts.
        gateway.health(timeout=6.0)
        with pytest.raises(DeadlineExceeded) as raised:
            _stream(gateway)
    message = str(raised.value)
    assert "total timeout fired after" in message, message
    # The budget is min(call timeout, remaining deadline), so it can print 0.9.
    assert re.search(r"exceeded its \d\.\ds budget", message), message


@pytest.mark.parametrize("make", GATEWAYS)
def test_a_fast_normal_stream_is_unchanged_with_and_without_limits(make):
    results = []
    for limits in ({}, {"stream_idle_seconds": 30.0, "stream_first_content_seconds": 30.0}):
        upstream = Upstream(content_script(["Hello", ", ", "world"], 0.0))
        with serving(upstream) as url:
            response, sink, _elapsed = _stream(make(url, **limits))
        results.append((response.text, "".join(sink.text)))
    assert results[0] == results[1] == ("Hello, world", "Hello, world")


def test_reasoning_deltas_count_as_first_content():
    # Passes only if the reasoning delta counted as first content; otherwise
    # the 2.0 s limit fires during the 2.5 s silence that follows it.
    script = [("event", chunk({"reasoning_content": "thinking"})), ("sleep", 2.5)]
    script += content_script(["ok"], 0.0)
    upstream = Upstream(script)
    with serving(upstream) as url:
        response, _sink, _elapsed = _stream(_sonder(url, stream_first_content_seconds=2.0,
                                                    stream_idle_seconds=10.0))
    assert response.text == "ok"


def test_limits_default_to_disabled_and_validate():
    config = config_from_env({})
    assert (config.stream_idle_seconds, config.stream_first_content_seconds) == (0.0, 0.0)
    config = config_from_env({ENV_STREAM_IDLE: "120", ENV_STREAM_FIRST_CONTENT: "900"})
    assert (config.stream_idle_seconds, config.stream_first_content_seconds) == (120.0, 900.0)
    for name in (ENV_STREAM_IDLE, ENV_STREAM_FIRST_CONTENT):
        for bad in ("-1", "86401", "nan", "soon"):
            with pytest.raises(InvalidInput):
                config_from_env({name: bad})
    with pytest.raises(InvalidInput):
        OpenAICompatibleConfig(base_url="http://127.0.0.1:1", stream_idle_seconds=-1)
    with pytest.raises(ValueError):
        sse_stream.StallLimits(idle=-1.0)


def test_openai_compat_env_settings_are_read(monkeypatch):
    from sonder_runtime.adapters.inference import openai_compat_gateway as compat

    monkeypatch.setenv(compat.ENV_STREAM_IDLE, "90")
    monkeypatch.setenv(compat.ENV_STREAM_FIRST_CONTENT, "600")
    config = compat._config_from_env()
    assert (config.stream_idle_seconds, config.stream_first_content_seconds) == (90.0, 600.0)
    monkeypatch.setenv(compat.ENV_STREAM_IDLE, "-5")
    with pytest.raises(InvalidInput):
        compat._config_from_env()


def test_openai_compat_does_not_stream_unless_the_caller_streams():
    upstream = Upstream(content_script(["x"], 0.0))
    with serving(upstream) as url:
        gateway = _compat(url)
        # No live stream: the historical non-streaming request body.
        seen: list[dict] = []

        def transport(target, payload, headers, timeout):
            seen.append(payload)
            return {"choices": [{"message": {"content": "plain"}, "finish_reason": "stop"}],
                    "model": "m1"}

        plain = OpenAICompatibleGateway(
            OpenAICompatibleConfig(base_url=url, model="m1"), transport=transport,
        )
        assert plain.generate(ModelRequest(prompt="hi", tier="general"), _ctx()).text == "plain"
        assert seen[0]["stream"] is False and "stream_options" not in seen[0]
        response, _sink, _elapsed = _stream(gateway)
    assert response.text == "x"
    assert upstream.requests[0]["stream_options"] == {"include_usage": True}


def test_a_bound_openai_compatible_tier_claims_the_stream_through_the_bridge():
    from sonder_runtime.application.chat import provider_bridge as bridge

    assert "openai_compatible" in bridge.STREAMING_PROVIDERS
    upstream = Upstream(content_script(["he", "llo"], 0.0))
    sink = Sink()
    live = stream_sink.LiveTurnStream(sink.write)
    with serving(upstream) as url:
        gateway = _compat(url, stream_idle_seconds=30.0)
        with stream_sink.armed(live), bridge.bind_rung("openai_compatible", "general"):
            bridge.generate_via_gateway(
                gateway, {"messages": [{"role": "user", "content": "hi"}]},
                tier="general", context=_ctx(),
            )
    assert live.claimed is True
    assert upstream.requests[0]["stream"] is True
    assert "".join(sink.text) == "hello"


@contextmanager
def _watch():
    # A whole-second base keeps the synthetic offsets exact in float, so the
    # strict ">" boundary below does not depend on the runner's clock value.
    started = float(int(time.monotonic()))
    watch = sse_stream._StallWatch(
        object(), sse_stream.StallLimits(idle=100.0, first_content=200.0), started, "endpoint",
    )
    try:
        yield watch, started
    finally:
        watch.stop()


def test_stall_watch_check_is_exact_with_a_synthetic_clock():
    # The background thread uses the real clock, so it never fires here.
    with _watch() as (watch, started):
        assert watch._check(started + 150) is None  # idle not armed yet
        first = watch._check(started + 201)
        assert first is not None and first.kind == "first_content"
        assert "after 201.0s (limit 200.0s" in str(first)
        assert "had not started (no response headers)" in str(first)

        watch._last_bytes = started
        assert watch._check(started + 100.0) is None  # strict ">"
        idle = watch._check(started + 100.5)
        assert idle is not None and idle.kind == "idle"
        assert "no bytes for 100.5s (limit 100.0s" in str(idle)
        assert "no content received yet" in str(idle)

        # Both exceeded with no content: idle wins (current precedence).
        both = watch._check(started + 500)
        assert both is not None and both.kind == "idle"

    with _watch() as (watch, started):
        watch.content()
        watch._last_bytes = started + 250
        assert watch._check(started + 300) is None
        idle = watch._check(started + 351)
        assert idle is not None and idle.kind == "idle"
        assert "first content already received" in str(idle)


def test_an_openai_compatible_stream_error_event_does_not_relay_provider_text():
    from sonder_runtime.domain.common.errors import DependencyUnavailable

    upstream = Upstream([("event", {"error": {"code": "boom", "message": "SECRET-TEXT"}})])
    with serving(upstream) as url:
        with pytest.raises(DependencyUnavailable) as raised:
            _stream(_compat(url))
    message = str(raised.value)
    assert "boom" in message and "SECRET-TEXT" not in message, message

    hostile = "x y\n" * 50
    upstream = Upstream([("event", {"error": {"code": hostile, "message": "SECRET-TEXT"}})])
    with serving(upstream) as url:
        with pytest.raises(DependencyUnavailable) as raised:
            _stream(_compat(url))
    message = str(raised.value)
    assert "SECRET-TEXT" not in message and "x y" not in message, message
    assert "failed the request while streaming (error)" in message, message


def test_stall_limits_follow_the_context_not_the_thread():
    seen: dict[str, object] = {}

    def probe(name):
        seen[name] = sse_stream.current_stall_limits()

    with sse_stream.stall_limits(3.0, 4.0):
        ctx = contextvars.copy_context()
        copied = threading.Thread(target=ctx.run, args=(probe, "copied"))
        plain = threading.Thread(target=probe, args=("plain",))
        for thread in (copied, plain):
            thread.start()
            thread.join(timeout=5)
        assert sse_stream.current_stall_limits().idle == 3.0
    assert seen["copied"].idle == 3.0 and seen["copied"].first_content == 4.0
    assert seen["plain"] == sse_stream.StallLimits()
    assert sse_stream.current_stall_limits() == sse_stream.StallLimits()
