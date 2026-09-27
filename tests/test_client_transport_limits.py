"""Response size and elapsed-time ceilings for the standalone client transport.

A real loopback server stands in for a compromised or broken Sonder server:
an oversized body, a response that never sends its body, and a body dripped
out slowly forever must each end the call with a clear error instead of
blocking the client or buffering without bound.
"""
from __future__ import annotations

import http.server
import json
import threading
import time
from contextlib import contextmanager


from sonder_runtime.adapters import client_transport

_KEY = "client-limit-fixture-key"


def _reply(size):
    return json.dumps({
        "choices": [{"message": {"content": "hi"}}], "pad": "x" * size,
    }).encode("utf-8")


@contextmanager
def _serve(write_body):
    stop = threading.Event()

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            try:
                write_body(self, stop)
            except OSError:
                pass

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1]
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _send_bounded(server, *, within):
    """Run send_chat_prompt on a thread; fail if it is still blocked later."""
    outcome = {}

    def run():
        try:
            outcome["value"] = client_transport.send_chat_prompt(server, _KEY, "hi")
        except BaseException as exc:  # noqa: BLE001 - reported to the test
            outcome["error"] = exc

    worker = threading.Thread(target=run, daemon=True)
    started = time.monotonic()
    worker.start()
    worker.join(timeout=within)
    assert not worker.is_alive(), "client call still blocked after %.1fs" % within
    outcome["elapsed"] = time.monotonic() - started
    return outcome


def _full(size):
    def write(handler, _stop):
        body = _reply(size)
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
    return write


def test_oversized_response_is_rejected_before_parsing(monkeypatch):
    monkeypatch.setattr(client_transport, "RESPONSE_BODY_LIMIT", 4096, raising=False)
    with _serve(_full(64 * 1024)) as server:
        outcome = _send_bounded(server, within=20)
    assert isinstance(outcome.get("error"), client_transport.ClientResponseLimitError)
    assert "exceeds" in str(outcome["error"])


def test_response_within_the_limit_is_returned(monkeypatch):
    monkeypatch.setattr(client_transport, "RESPONSE_BODY_LIMIT", 64 * 1024, raising=False)
    with _serve(_full(1024)) as server:
        outcome = _send_bounded(server, within=20)
    assert outcome.get("value") == "hi"


def test_stalled_response_times_out(monkeypatch):
    monkeypatch.setattr(client_transport, "REQUEST_TIMEOUT_SECONDS", 1.0, raising=False)

    def stall(handler, stop):
        handler.send_response(200)
        handler.send_header("Content-Length", "100")
        handler.end_headers()
        handler.wfile.flush()
        stop.wait(30)

    with _serve(stall) as server:
        outcome = _send_bounded(server, within=15)
    assert isinstance(outcome.get("error"), (TimeoutError, OSError))


def test_dripped_response_hits_the_total_deadline(monkeypatch):
    monkeypatch.setattr(client_transport, "REQUEST_TIMEOUT_SECONDS", 2.0, raising=False)
    monkeypatch.setattr(client_transport, "REQUEST_DEADLINE_SECONDS", 1.5, raising=False)

    def drip(handler, stop):
        handler.send_response(200)
        handler.send_header("Content-Length", str(10_000_000))
        handler.end_headers()
        while not stop.is_set():
            handler.wfile.write(b" ")
            handler.wfile.flush()
            time.sleep(0.2)

    with _serve(drip) as server:
        outcome = _send_bounded(server, within=15)
    assert isinstance(outcome.get("error"), client_transport.ClientResponseLimitError)
    assert "deadline" in str(outcome["error"])
