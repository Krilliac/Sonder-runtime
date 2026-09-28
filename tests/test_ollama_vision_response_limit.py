"""The loopback Ollama vision transport reads a bounded successful body."""
from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager

import pytest

from sonder_runtime.adapters.inference import ollama_vision
from sonder_runtime.domain.common.errors import DependencyUnavailable


@contextmanager
def _serve(body):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass

        def log_message(self, *_args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d/api/chat" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _body(pad):
    return json.dumps({"message": {"content": "a cat"}, "pad": "x" * pad}).encode("utf-8")


def test_oversized_vision_response_is_rejected(monkeypatch):
    monkeypatch.setattr(ollama_vision, "RESPONSE_BODY_LIMIT", 4096, raising=False)
    with _serve(_body(64 * 1024)) as url:
        with pytest.raises(DependencyUnavailable, match="exceeds"):
            ollama_vision.OllamaVisionGateway._post(url, {"model": "m"}, 10)


def test_vision_response_within_the_limit_is_parsed(monkeypatch):
    monkeypatch.setattr(ollama_vision, "RESPONSE_BODY_LIMIT", 64 * 1024, raising=False)
    with _serve(_body(1024)) as url:
        data = ollama_vision.OllamaVisionGateway._post(url, {"model": "m"}, 10)
    assert data["message"]["content"] == "a cat"
