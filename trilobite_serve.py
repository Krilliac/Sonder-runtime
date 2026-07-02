"""trilobite_serve — OpenAI-compatible HTTP proxy in front of the real trilobite loop.

Lets any OpenAI-compatible chat UI (Open WebUI, etc.) talk to server.trilobite()
instead of raw Ollama, including the REPL's slash-command powers (/stats, /pass,
/fail, /trace, /strict). Stdlib only (http.server / json / urllib) — zero-dep,
matching the rest of this project.

Run:
    ./venv/Scripts/python.exe trilobite_serve.py [port]
    (or set env TRILOBITE_PORT; default 11435)

Point your chat UI's OpenAI API base at http://127.0.0.1:<port>/v1 (any api key).
"""
import json
import os
import sys
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import server

DEFAULT_PORT = 11435

# Server state (module globals, single-user local — mirrors trilobite_repl.py).
TRACE = False
STRICT = None  # None = env default (server._STRICT_DEFAULT)
LAST_IID = None

HELP_TEXT = """commands:
  /help              show this help
  /trace [on|off]    toggle trace mode (bare = on); shows retrieval + prompt
  /strict [on|off]   toggle strict mode (bare = on); pins to the trilobite alias
  /stats             show trilobite's learning stats
  /pass, /good       record the last answer as tests_passed
  /fail, /bad        record the last answer as failed
"""


def _strip_footer(text):
    idx = text.find(server.FOOTER_PREFIX)
    if idx == -1:
        return text
    return text[:idx]


def _on_off(arg, current):
    arg = (arg or "").strip().lower()
    if arg in ("", "on"):
        return True
    if arg == "off":
        return False
    return current


def _last_user_message(messages):
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            return msg.get("content") or ""
    return ""


def _handle_slash(content):
    """Return response text if `content` is a recognized slash command, else None."""
    global TRACE, STRICT, LAST_IID

    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return HELP_TEXT
    if cmd == "/stats":
        return server.trilobite_stats()
    if cmd in ("/pass", "/good"):
        if LAST_IID:
            return server.record_outcome(LAST_IID, "tests_passed")
        return "(nothing to record yet)"
    if cmd in ("/fail", "/bad"):
        if LAST_IID:
            return server.record_outcome(LAST_IID, "failed")
        return "(nothing to record yet)"
    if cmd == "/trace":
        TRACE = _on_off(arg, TRACE)
        return "trace %s" % ("on" if TRACE else "off")
    if cmd == "/strict":
        STRICT = _on_off(arg, STRICT)
        return "strict %s" % ("on" if STRICT else "off")

    return None  # not a recognized slash command — fall through to the model


def _run_prompt(prompt):
    """Call the real trilobite loop; returns the text shown to the UI."""
    global LAST_IID

    out = server.trilobite(prompt, trace=TRACE, strict=STRICT)
    if out.startswith("ERROR"):
        return out
    LAST_IID = server.parse_interaction_id(out)
    return _strip_footer(out)


def _chat_completion_object(content, model="trilobite"):
    iid = LAST_IID or uuid.uuid4().hex[:12]
    return {
        "id": "chatcmpl-%s" % iid,
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def _chunk(iid, model, delta, finish_reason=None):
    obj = {
        "id": "chatcmpl-%s" % iid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    return "data: %s\n\n" % json.dumps(obj)


class Handler(BaseHTTPRequestHandler):
    server_version = "trilobite-serve/1.0"

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def log_message(self, fmt, *args):
        sys.stderr.write("[trilobite_serve] %s\n" % (fmt % args))

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path.rstrip("/") == "/v1/models":
            body = json.dumps({
                "object": "list",
                "data": [{"id": "trilobite", "object": "model", "owned_by": "local"}],
            }).encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self._cors()
        self.end_headers()

    def do_POST(self):
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        try:
            length = int(self.headers.get("Content-Length", 0) or 0)
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:
            self._send_error_completion("ERROR parsing request body: %s" % e, stream=False)
            return

        messages = req.get("messages", [])
        stream = bool(req.get("stream", False))
        model = req.get("model", "trilobite")
        prompt = _last_user_message(messages)

        try:
            slash_reply = _handle_slash(prompt)
            content = slash_reply if slash_reply is not None else _run_prompt(prompt)
        except Exception:
            content = "ERROR: %s" % traceback.format_exc()

        if stream:
            self._send_stream(content, model)
        else:
            self._send_json(_chat_completion_object(content, model))

    def _send_error_completion(self, text, stream):
        if stream:
            self._send_stream(text, "trilobite")
        else:
            self._send_json(_chat_completion_object(text, "trilobite"))

    def _send_json(self, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_stream(self, content, model):
        iid = LAST_IID or uuid.uuid4().hex[:12]
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        # No Content-Length on an SSE body — signal end-of-response by closing the
        # connection, otherwise HTTP/1.1 keep-alive leaves clients blocked on read().
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            self.wfile.write(_chunk(iid, model, {"role": "assistant", "content": content}).encode("utf-8"))
            self.wfile.write(_chunk(iid, model, {}, finish_reason="stop").encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            pass


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    else:
        port = int(os.environ.get("TRILOBITE_PORT", DEFAULT_PORT))

    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    url = "http://127.0.0.1:%d" % port
    print("trilobite_serve listening on %s" % url)
    print("point your chat UI's OpenAI API base at %s/v1 (any api key)" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
