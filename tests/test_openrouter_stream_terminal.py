"""Real loopback terminal controls; no provider, weights or real credentials."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from types import SimpleNamespace

import pytest

from sonder_runtime.adapters.inference import openrouter_gateway as module
from sonder_runtime.adapters.persistence.session_repository import SQLiteSessionRepository
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.session.capture import SessionCaptureService
from sonder_runtime.application.session import provider_attempts
from sonder_runtime.domain.common.errors import DependencyUnavailable
from tests.test_openrouter_stream_backpressure import context, gateway, observed as observed


CASES = (
    ("complete", 2, "complete", "content-length"),
    ("eof_no_marker", 2, "unfinished", "content-length"),
    ("eof_close_delimited", 2, "unfinished", "close-delimited"),
    ("eof_declared_body_short", 2, "unfinished", "short-body"),
    ("eof_after_finish_usage", 2, "finish", "content-length"),
    ("eof_full_backlog", 128, "unfinished", "close-delimited"),
    ("explicit_error", 2, "error", "content-length"),
    ("malformed_event", 2, "malformed", "content-length"),
    ("done_without_finish", 2, "done", "content-length"),
    ("eof_without_deltas", 0, "unfinished", "content-length"),
)


def _frame(document):
    return b"data: " + json.dumps(document, ensure_ascii=False).encode() + b"\n\n"


def _body(count, terminal):
    body = b"".join(_frame({"choices": [{"delta": {"content": "%dλ" % i}}]})
                    for i in range(count))
    if terminal in ("complete", "finish"):
        body += _frame({"choices": [{"delta": {}, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 3, "completion_tokens": count, "cost": 0}})
    if terminal == "error":
        body += _frame({"error": {"message": "synthetic upstream failure"}})
    elif terminal == "malformed":
        body += b"data: {\n\n"
    if terminal in ("complete", "done"):
        body += b"data: [DONE]\n\n"
    return body


@pytest.fixture
def terminal_peer(request):
    name, count, terminal, framing = request.param
    body = _body(count, terminal)
    sends = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            sends.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            if framing != "close-delimited":
                self.send_header("Content-Length", str(len(body) + (100 if framing == "short-body" else 0)))
            self.end_headers()
            try:
                self.wfile.write(body)
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass  # a control may intentionally abandon its iterator
            self.close_connection = True

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    peer = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    peer.start()
    try:
        yield SimpleNamespace(name=name, count=count, terminal=terminal, sends=sends,
                              url="http://127.0.0.1:%d/api/v1" % server.server_port)
    finally:
        server.shutdown()
        server.server_close()
        peer.join(3)
        assert not peer.is_alive()


class Observer:
    def __init__(self):
        self.starts = []
        self.finishes = []

    def provider_send_started(self, *args):
        self.starts.append(args)
        return len(self.starts)

    def provider_send_finished(self, handle, **facts):
        self.finishes.append(facts)


def _capture(tmp_path):
    repository = SQLiteSessionRepository(tmp_path / "session.db")
    capture = SessionCaptureService(repository)
    request = ModelRequest("synthetic local audit prompt", "code", stream=True)
    pending = capture.begin_request("session", "turn", request, request_id="request")
    return repository, capture, request, pending


@pytest.mark.parametrize("terminal_peer", CASES, indirect=True, ids=[case[0] for case in CASES])
def test_real_http_requires_done_before_normal_completion(terminal_peer, observed, tmp_path, monkeypatch):
    usage, chunks = [], []
    observer = Observer()
    monkeypatch.setattr(module, "record_usage", usage.append)
    monkeypatch.setattr(provider_attempts, "_attempt_observer", observer)
    adapter = gateway(None)
    adapter._env = {**adapter._env, "SONDER_OPENROUTER_BASE_URL": terminal_peer.url}
    repository, capture, request, pending = _capture(tmp_path)
    stream = adapter.stream(request, context())
    incomplete = terminal_peer.name.startswith("eof_")
    expects_error = incomplete or terminal_peer.terminal in ("error", "malformed")
    try:
        with provider_attempts.provider_attempt_scope(capture, pending):
            if terminal_peer.name == "eof_full_backlog":
                chunks.append(next(stream))
                assert observed.full.wait(3)
            if expects_error:
                with pytest.raises(DependencyUnavailable) as failure:
                    for chunk in stream:
                        chunks.append(chunk)
                if incomplete:
                    assert str(failure.value) == "OpenRouter stream ended before [DONE]"
            else:
                chunks.extend(stream)
    finally:
        stream.close()

    assert [chunk.text for chunk in chunks if chunk.text] == ["%dλ" % i for i in range(terminal_peer.count)]
    final = [chunk for chunk in chunks if not chunk.text]
    events = repository.read_range("session")
    assert [event.event_type for event in events] == [
        "model.requested", "provider.requested", "provider.failed" if expects_error else "provider.responded",
    ]
    assert len(terminal_peer.sends) == len(observer.starts) == len(observer.finishes) == 1
    assert terminal_peer.sends[0]["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": True}
    assert observed.queues[0].high_water <= 64
    assert not observed.workers[0].is_alive()
    if expects_error:
        assert final == usage == []
        assert adapter.last_usage is None
        assert events[-1].payload["error_code"] == observer.finishes[0]["error_code"] == "DEPENDENCY_UNAVAILABLE"
    else:
        assert len(final) == len(usage) == 1
        assert final[0].finish_reason == ("stop" if terminal_peer.terminal == "complete" else None)
        assert "error_code" not in observer.finishes[0]
        if terminal_peer.terminal == "complete":
            assert (final[0].input_tokens, final[0].output_tokens) == (3, terminal_peer.count)


@pytest.mark.parametrize("terminal_peer", [CASES[0]], indirect=True)
def test_completed_stream_accounts_once_before_capture_failure(terminal_peer, observed, tmp_path, monkeypatch):
    usage, chunks = [], []
    monkeypatch.setattr(module, "record_usage", usage.append)
    adapter = gateway(None)
    adapter._env = {**adapter._env, "SONDER_OPENROUTER_BASE_URL": terminal_peer.url}
    repository, capture, request, pending = _capture(tmp_path)

    def fail_capture(*args, **kwargs):
        raise OSError("synthetic evidence storage failure")

    monkeypatch.setattr(capture, "finish_provider_attempt", fail_capture)
    stream = adapter.stream(request, context())
    try:
        with pytest.raises(provider_attempts.ProviderCaptureFailure):
            with provider_attempts.provider_attempt_scope(capture, pending):
                for chunk in stream:
                    chunks.append(chunk)
    finally:
        stream.close()
    assert [chunk.text for chunk in chunks] == ["%dλ" % i for i in range(terminal_peer.count)]
    assert len(terminal_peer.sends) == len(usage) == 1
    assert usage[0]["cost_usd"] == 0
    assert (usage[0]["prompt_tokens"], usage[0]["completion_tokens"]) == (3, terminal_peer.count)
    assert adapter.last_usage == usage[0]
    assert [event.event_type for event in repository.read_range("session")] == ["model.requested", "provider.requested"]
    assert not observed.workers[0].is_alive()
