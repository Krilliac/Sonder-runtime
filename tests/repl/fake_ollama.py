"""A deterministic stand-in for Ollama, for REPL screen tests.

Serves only what a REPL chat turn touches (``/api/tags``, ``/api/version``,
``/api/ps``, ``/api/show``, ``/api/chat``, ``/api/generate`` and
``/api/embed[dings]``) with canned answers chosen by keywords in the last
user message.  No model runs and nothing leaves loopback.

Keywords (case-insensitive) in the prompt:

* ``raii``    -> a fixed one-sentence answer.
* ``inject``  -> an answer carrying ESC/OSC 52/BEL/C1 CSI/U+202E, to prove
  the REPL sanitizes model text.
* ``hold``    -> the chat call blocks until :meth:`FakeOllama.release` (or
  30 s), so a test can snapshot the live line of a turn in progress.
* ``explode`` -> an Ollama ``{"error": ...}`` reply, which the runtime turns
  into an error result.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODELS = (
    ("sonder:latest", 900_000_000),
    ("qwen2.5:0.5b", 400_000_000),
    ("nomic-embed-text:latest", 300_000_000),
)

RAII_ANSWER = (
    "RAII stands for Resource Acquisition Is Initialization, a C++ technique"
    " where a resource is acquired in a constructor and released in the"
    " destructor, so cleanup happens automatically when the object leaves"
    " scope."
)
INJECT_ANSWER = (
    "safe start \x1b]52;c;SGVsbG8=\x07 clip \x1b[2J wipe \x9b31m csi"
    " ‮evil‬ end"
)


class FakeOllama:
    """A threaded HTTP server; ``url`` is ready once constructed."""

    def __init__(self):
        self.requests = []
        self._release = threading.Event()
        self.holding = threading.Event()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                return

            def _send(self, status, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                owner.requests.append(("GET", self.path, None))
                if self.path.startswith("/api/tags"):
                    return self._send(200, {"models": [
                        {"name": name, "model": name, "size": size,
                         "details": {"family": "fake"}}
                        for name, size in MODELS
                    ]})
                if self.path.startswith("/api/version"):
                    return self._send(200, {"version": "0.0.0-fake"})
                if self.path.startswith("/api/ps"):
                    return self._send(200, {"models": []})
                return self._send(404, {"error": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    payload = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    payload = {}
                owner.requests.append(("POST", self.path, payload))
                if self.path.startswith("/api/show"):
                    return self._send(200, {
                        "capabilities": ["completion"],
                        "model_info": {"general.context_length": 8192},
                        "details": {"family": "fake"},
                    })
                if self.path.startswith("/api/embed"):
                    vector = [0.0] * 8
                    if self.path.startswith("/api/embeddings"):
                        return self._send(200, {"embedding": vector})
                    return self._send(200, {"embeddings": [vector]})
                if self.path.startswith("/api/chat"):
                    return self._chat(payload)
                if self.path.startswith("/api/generate"):
                    if not payload.get("prompt"):
                        return self._send(200, {"model": payload.get("model", ""),
                                                "response": "", "done": True})
                    text = owner.answer(str(payload.get("prompt") or ""))
                    return self._send(200, {"model": payload.get("model", ""),
                                            "response": text, "done": True,
                                            "prompt_eval_count": 12, "eval_count": 8})
                return self._send(404, {"error": "not found"})

            def _chat(self, payload):
                messages = payload.get("messages") or []
                last = ""
                for message in messages:
                    if message.get("role") == "user":
                        last = str(message.get("content") or "")
                lowered = last.lower()
                if "explode" in lowered:
                    return self._send(500, {"error": "fake model crashed"})
                if "hold" in lowered:
                    owner.holding.set()
                    owner._release.wait(30)
                text = owner.answer(last)
                return self._send(200, {
                    "model": payload.get("model", ""),
                    "message": {"role": "assistant", "content": text},
                    "done": True, "done_reason": "stop",
                    "prompt_eval_count": 2600, "eval_count": 43,
                })

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.url = "http://127.0.0.1:%d" % self._server.server_address[1]

    @staticmethod
    def answer(prompt):
        lowered = prompt.lower()
        if "inject" in lowered:
            return INJECT_ANSWER
        if "raii" in lowered or "hold" in lowered:
            return RAII_ANSWER
        return "A canned answer from the fake model."

    def release(self):
        self._release.set()

    def close(self):
        self._release.set()
        self._server.shutdown()
        self._server.server_close()


__all__ = ["FakeOllama", "INJECT_ANSWER", "RAII_ANSWER"]
