"""Actual HTTP cooperative-close boundary, with one gated synthetic peer."""
from __future__ import annotations

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

from sonder_runtime.adapters.inference import openrouter_gateway as module
from sonder_runtime.application.ports.model_gateway import ModelRequest
from tests.test_openrouter_stream_backpressure import context, gateway


def test_close_returns_with_socket_blocked_then_peer_release_drains_worker(monkeypatch, caplog):
    release = threading.Event()
    blocked_read = threading.Event()
    requests, workers = [], []
    original_thread = module.owned_runtime_thread
    usage = []

    def make_thread(*args, **kwargs):
        result = original_thread(*args, **kwargs)
        workers.append(result)
        return result

    def transport(*args):
        source = module._default_stream(*args)
        reads = 0
        try:
            while True:
                reads += 1
                if reads == 3:
                    # First data line and blank separator have been read.
                    # The peer withholds every remaining byte behind release.
                    blocked_read.set()
                try:
                    yield next(source)
                except StopIteration:
                    return
        finally:
            source.close()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            self.wfile.write(b'data: {"choices":[{"delta":{"content":"synthetic"}}]}\n\n')
            self.wfile.flush()
            release.wait(5)
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, *args):
            pass

    monkeypatch.setattr(module, "owned_runtime_thread", make_thread)
    monkeypatch.setattr(module, "record_usage", usage.append)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    peer = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01})
    peer.start()
    adapter = gateway(transport)
    adapter._env = {**adapter._env,
                    "SONDER_OPENROUTER_BASE_URL": "http://127.0.0.1:%d/api/v1" % server.server_port,
                    "SONDER_OPENROUTER_TIMEOUT_SECONDS": "3"}
    stream = adapter.stream(ModelRequest("synthetic private canary", "code"), context())
    try:
        assert next(stream).text == "synthetic"
        assert blocked_read.wait(3)
        started = time.monotonic()
        stream.close()
        assert time.monotonic() - started < 1, "close waited for socket timeout"
        assert workers[0].is_alive(), "close must not claim synchronous socket cancellation"
        release.set()
        workers[0].join(3)
        assert not workers[0].is_alive()
        assert len(requests) == 1
        assert requests[0]["provider"] == {"data_collection": "deny", "zdr": True, "allow_fallbacks": True}
        assert usage == []
        assert "synthetic-test-only" not in caplog.text
        assert "synthetic private canary" not in caplog.text
    finally:
        release.set()
        stream.close()
        for worker in workers:
            worker.join(3)
            assert not worker.is_alive()
        server.shutdown()
        server.server_close()
        peer.join(3)
        assert not peer.is_alive()
