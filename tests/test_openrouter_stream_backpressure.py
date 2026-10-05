"""Synthetic SSE stability controls; no provider, weights or real credentials."""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import queue
import threading
import time
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference import openrouter_gateway as module
from sonder_runtime.adapters.model_request_admission import HostModelRequestAdmission
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.domain.common.errors import Cancelled, DeadlineExceeded, DependencyUnavailable


class Cancellation:
    def __init__(self):
        self.event = threading.Event()

    @property
    def cancelled(self):
        return self.event.is_set()

    def wait(self, timeout=None):
        return self.event.wait(timeout)


@pytest.fixture
def observed(monkeypatch):
    queues, workers = [], []
    full = threading.Event()

    class ObservedQueue(queue.Queue):
        high_water = 0

        def put(self, item, block=True, timeout=None):
            super().put(item, block, timeout)
            self.high_water = max(self.high_water, self.qsize())
            if self.qsize() >= 64:
                full.set()

    def make_queue(*args, **kwargs):
        result = ObservedQueue(*args, **kwargs)
        queues.append(result)
        return result

    def make_thread(*args, **kwargs):
        result = threading.Thread(*args, **kwargs)
        workers.append(result)
        return result

    monkeypatch.setattr(module, "queue", SimpleNamespace(
        Queue=make_queue, Empty=queue.Empty, Full=queue.Full,
    ))
    monkeypatch.setattr(module, "owned_runtime_thread", make_thread)
    yield SimpleNamespace(queues=queues, workers=workers, full=full)
    for worker in workers:
        worker.join(3)
        assert not worker.is_alive(), "synthetic stream worker did not drain"


def gateway(transport):
    return module.OpenRouterGateway(
        stream_transport=transport, policy_models=None,
        request_admission=HostModelRequestAdmission(), env={
            "SONDER_ALLOW_CLOUD": "1", "OPENROUTER_API_KEY": "synthetic-test-only",
            "SONDER_OPENROUTER_BASE_URL": "http://127.0.0.1:1/api/v1",
            "SONDER_OPENROUTER_MODEL": "synthetic/model",
        },
    )


def context(**kwargs):
    return local_owner_context(correlation_id="synthetic-stream", cloud_allowed=True,
                               timeout_seconds=10, **kwargs)


def frames(count, *, fail=False, closed=None, calls=None):
    def transport(_url, _payload, _headers, _timeout):
        if calls is not None:
            calls.append(1)
        try:
            for index in range(count):
                yield ("data: " + json.dumps({"choices": [{"delta": {
                    "content": "%dλ" % index,
                }}]})).encode()
            if fail:
                yield b'data: {"error":{"message":"synthetic failure"}}'
            else:
                yield b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"cost":0}}'
                yield b"data: [DONE]"
        finally:
            if closed is not None:
                closed.set()
    return transport


def test_paused_consumer_has_bounded_chunk_backlog_and_close_drains(observed):
    closed, calls = threading.Event(), []
    stream = gateway(frames(2048, closed=closed, calls=calls)).stream(
        ModelRequest("synthetic private canary", "code"), context(),
    )
    try:
        assert next(stream).text == "0λ"
        assert observed.full.wait(3)
        assert observed.queues[0].high_water <= 64
        assert not closed.is_set(), "producer should pause instead of buffering everything"
    finally:
        stream.close()
    assert closed.wait(1)
    assert calls == [1]


@pytest.mark.parametrize("control", ["cancel", "deadline"])
def test_full_backlog_obeys_control_before_delivering_more_text(observed, control):
    cancel = Cancellation()
    ctx = context(cancellation=cancel)
    stream = gateway(frames(2048)).stream(ModelRequest("synthetic", "code"), ctx)
    try:
        assert next(stream).text == "0λ"
        assert observed.full.wait(3)
        if control == "cancel":
            cancel.event.set()
        else:
            object.__setattr__(ctx, "deadline_monotonic", time.monotonic() - 1)
        with pytest.raises(Cancelled if control == "cancel" else DeadlineExceeded):
            next(stream)
    finally:
        stream.close()


@pytest.mark.parametrize("count", [1, 64, 2048])
def test_terminal_notification_preserves_every_chunk_in_order(observed, count):
    calls = []
    chunks = list(gateway(frames(count, calls=calls)).stream(
        ModelRequest("synthetic", "code"), context(),
    ))
    assert [chunk.text for chunk in chunks[:-1]] == ["%dλ" % i for i in range(count)]
    assert chunks[-1].finish_reason == "stop"
    assert chunks[-1].input_tokens == chunks[-1].output_tokens == 1
    assert observed.queues[0].high_water <= 64
    assert calls == [1]


def test_empty_completion_preserves_existing_output_validation(observed):
    with pytest.raises(DependencyUnavailable, match="no usable text"):
        list(gateway(frames(0)).stream(ModelRequest("synthetic", "code"), context()))


def test_full_backlog_preserves_buffered_chunks_then_delivers_worker_error(observed):
    stream = gateway(frames(128, fail=True)).stream(ModelRequest("synthetic", "code"), context())
    texts = [next(stream).text]
    try:
        assert observed.full.wait(3)
        with pytest.raises(DependencyUnavailable, match="synthetic failure"):
            for chunk in stream:
                texts.append(chunk.text)
    finally:
        stream.close()
    assert texts == ["%dλ" % i for i in range(128)]


@pytest.fixture
def http_peer():
    requests = []
    body = b"\n\n".join(frames(2048)(None, None, None, None)) + b"\n\n"

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # deliberate iterator abandonment closes its connection

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield SimpleNamespace(url="http://127.0.0.1:%d/api/v1" % server.server_port,
                              requests=requests)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(3)
        assert not thread.is_alive()


@pytest.mark.parametrize("action", ["drain", "close", "cancel", "deadline"])
def test_real_http_slow_consumer_single_send_privacy_and_cleanup(observed, http_peer, monkeypatch, action):
    usage = []
    monkeypatch.setattr(module, "record_usage", usage.append)
    adapter = gateway(None)
    adapter._env = {**adapter._env, "SONDER_OPENROUTER_BASE_URL": http_peer.url}
    cancel = Cancellation()
    ctx = context(cancellation=cancel)
    stream = adapter.stream(ModelRequest("synthetic private canary", "code"), ctx)
    try:
        assert next(stream).text == "0λ"
        assert observed.full.wait(3)
        assert observed.queues[0].high_water <= 64
        if action == "drain":
            chunks = list(stream)
            assert [chunk.text for chunk in chunks[:-1]] == ["%dλ" % i for i in range(1, 2048)]
            assert chunks[-1].finish_reason == "stop"
            assert len(usage) == 1 and usage[0]["cost_usd"] == 0
        elif action != "close":
            if action == "cancel":
                cancel.event.set()
            else:
                object.__setattr__(ctx, "deadline_monotonic", time.monotonic() - 1)
            with pytest.raises(Cancelled if action == "cancel" else DeadlineExceeded):
                next(stream)
    finally:
        stream.close()
    assert len(http_peer.requests) == 1
    assert http_peer.requests[0]["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": True}
    if action != "drain":
        assert usage == []
    assert not observed.workers[0].is_alive()
