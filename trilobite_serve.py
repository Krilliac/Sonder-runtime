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
import grounding
import code_runner
import training_tasks
import intents
import feedback
import live_reload

DEFAULT_PORT = 11435

# Auth + bind config. Empty API_KEY = auth disabled (local-only default).
API_KEY = os.environ.get("TRILOBITE_API_KEY", "")
HOST = os.environ.get("TRILOBITE_HOST", "127.0.0.1")

# Server state (module globals, single-user local — mirrors trilobite_repl.py).
TRACE = False
STRICT = None  # None = env default (server._STRICT_DEFAULT)
LAST_IID = None
LAST_RESPONSE = None  # full last assistant turn (with footer), for /run

TRAIN_DEFAULT_N = 3


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


TRAIN_MAX_N = max(1, _env_int("TRILOBITE_TRAIN_MAX_N", 500))

LIVE_RELOAD_MODULES = [
    "server",
    "grounding",
    "training_tasks",
    "intents",
    "feedback",
    "emotion_vectors",
    "web_tools",
]

HELP_TEXT = """commands:
  /help              show this help
  /trace [on|off]    toggle trace mode (bare = on); shows retrieval + prompt
  /strict [on|off]   toggle strict mode (bare = on); pins to the trilobite alias
  /stats             show trilobite's learning stats
  /pass, /good       record the last answer as tests_passed
  /fail, /bad        record the last answer as failed
  /run [seconds]     execute the code block from the last response (default 8s)
  /runproject [sec]  execute file/path fenced blocks as a temp project
  /train, /learn [N] grounded self-learning: practice N tasks (default 3, max 500)

Plain English also works for the toggles/actions above, e.g. "strict on,
show your reasoning", "run it", "train yourself".
"""


def check_auth(auth_header, api_key):
    """Pure auth check. True if api_key is empty (auth disabled), else True only if
    auth_header is "Bearer <api_key>" (or a raw match to api_key, for convenience)."""
    if not api_key:
        return True
    auth_header = auth_header or ""
    if auth_header == "Bearer " + api_key:
        return True
    if auth_header == api_key:
        return True
    return False


def _strip_footer(text):
    idx = text.find(server.FOOTER_PREFIX)
    if idx == -1:
        return text
    return text[:idx]


def _maybe_live_reload():
    global server, grounding, training_tasks, intents, feedback
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    server = modules.get("server", server)
    grounding = modules.get("grounding", grounding)
    training_tasks = modules.get("training_tasks", training_tasks)
    intents = modules.get("intents", intents)
    feedback = modules.get("feedback", feedback)


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


def _history_from_messages(messages):
    """Prior user/assistant turns from the UI request, excluding the current (last
    user) message. The chat UI owns conversation state here, so we thread exactly
    what it sends rather than a DB session."""
    msgs = messages or []
    last_user_idx = None
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i].get("role") == "user":
            last_user_idx = i
            break
    history = []
    for i, m in enumerate(msgs):
        if i == last_user_idx:
            continue
        role = m.get("role")
        content = m.get("content") or ""
        if role in ("user", "assistant") and content:
            history.append({"role": role, "content": content})
    return history


def _parse_train_n(arg):
    """Parse /train's N argument. Returns (n, error_message); n is None on error."""
    arg = (arg or "").strip()
    if not arg:
        return TRAIN_DEFAULT_N, None
    try:
        n = int(arg)
    except ValueError:
        return None, "usage: /train [N]  (N must be an integer, default %d)" % TRAIN_DEFAULT_N
    if n < 1:
        n = 1
    if n > TRAIN_MAX_N:
        n = TRAIN_MAX_N
    return n, None


def _parse_run_timeout(arg):
    arg = (arg or "").strip()
    if not arg:
        return grounding.DEFAULT_TIMEOUT, None
    try:
        value = int(arg)
    except ValueError:
        return None, "usage: /run [seconds]  (runs the previous fenced code block, not a filename or shell command)"
    return grounding.clamp_timeout(value), None


def _do_run(timeout=grounding.DEFAULT_TIMEOUT):
    """Execute the code block from LAST_RESPONSE via grounding. Mirrors the REPL's /run."""
    block = grounding.extract_runnable_code_block(LAST_RESPONSE)
    if block is None:
        return "(no code block in the last response to run)"
    result = code_runner.run_code(
        block["code"],
        language=block["language"],
        timeout=timeout,
    )
    if result.get("ok"):
        status = "[ran OK]"
    elif result.get("returncode") is None and result.get("error", "").startswith("timed out"):
        status = "[timed out]"
    else:
        status = "[exited with error]"
    return "%s\n%s" % (code_runner.format_result(result), status)


def _do_runproject(timeout=grounding.MAX_TIMEOUT):
    files = grounding.extract_project_files(LAST_RESPONSE)
    if not files:
        return "(no file/path fenced project blocks in the last response)"
    result = code_runner.run_project({"files": files}, timeout=timeout)
    status = "[ran OK]" if result.get("ok") else "[project failed]"
    return "%s\n%s" % (code_runner.format_project_result(result), status)


def _do_train(n):
    """Run a grounded self-training pass over n practice tasks. Mirrors the REPL's /train N."""
    tasks = training_tasks.sample(n)
    passed = 0
    lessons = 0
    lines = []
    for t in tasks:
        lines.append("  training: %s ..." % t["name"])
        resp = server.trilobite(t["prompt"])
        iid = server.parse_interaction_id(resp)
        code = grounding.extract_code_block(resp)
        ok = False
        if code:
            ok, _ = grounding.run_code(code, t["check"])
        signal = "tests_passed" if ok else "failed"
        passed += 1 if ok else 0
        if iid:
            msg = server.record_outcome(iid, signal)
            if "Distilled lesson" in msg:
                lessons += 1
            lines.append("    -> %s  %s" % ("PASS" if ok else "FAIL", msg))
        else:
            lines.append("    -> %s (no id)" % ("PASS" if ok else "FAIL"))
    lines.append("trained on %d tasks: %d passed, %d failed, %d new lessons" % (
        len(tasks), passed, len(tasks) - passed, lessons))
    return "\n".join(lines)


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
            msg = server.record_outcome(LAST_IID, "tests_passed")
            LAST_IID = None
            return msg
        return "(nothing to record yet)"
    if cmd in ("/fail", "/bad"):
        if LAST_IID:
            msg = server.record_outcome(LAST_IID, "failed")
            LAST_IID = None
            return msg
        return "(nothing to record yet)"
    if cmd == "/trace":
        TRACE = _on_off(arg, TRACE)
        return "trace %s" % ("on" if TRACE else "off")
    if cmd == "/strict":
        STRICT = _on_off(arg, STRICT)
        return "strict %s" % ("on" if STRICT else "off")
    if cmd == "/run":
        timeout, err = _parse_run_timeout(arg)
        if err:
            return err
        return _do_run(timeout)
    if cmd == "/runproject":
        timeout, err = _parse_run_timeout(arg)
        if err:
            return err
        return _do_runproject(timeout)
    if cmd in ("/train", "/learn"):
        n, err = _parse_train_n(arg)
        if err:
            return err
        return _do_train(n)

    return None  # not a recognized slash command — fall through to the model


def _handle_feedback(content):
    """Passive learning: if `content` reads as plain feedback on the last turn
    ("thanks, that worked" / "no that's wrong") rather than a new task, record
    the outcome on LAST_IID and return an acknowledgement. Else None (fall
    through to intent/model handling)."""
    global LAST_IID

    if not LAST_IID:
        return None

    fb = feedback.classify_feedback(content)
    if fb == "positive":
        server.record_outcome(LAST_IID, "accepted")
        LAST_IID = None
        return "Got it — recorded that as helpful so I can learn."
    if fb == "negative":
        server.record_outcome(LAST_IID, "rejected")
        LAST_IID = None
        return "Got it — recorded that as not-helpful so I can learn."
    return None


def _handle_intent(content):
    """Return response text if `content` is a natural-language control intent, else None."""
    global TRACE, STRICT

    intent = intents.classify(content)
    if not intent:
        return None

    replies = []
    if "trace" in intent:
        TRACE = intent["trace"]
        replies.append("trace %s" % ("on" if TRACE else "off"))
    if "strict" in intent:
        STRICT = intent["strict"]
        replies.append("strict %s" % ("on" if STRICT else "off"))
    if intent.get("run"):
        replies.append(_do_run())
    if "train" in intent:
        replies.append(_do_train(intent["train"]))
    return "\n".join(replies)


def _model_to_tier(model):
    """Map an OpenAI `model` field to a trilobite tier for answer_with_history.

    "trilobite" (and the OpenAI-ish default "gpt-*"/blank) -> the local student.
    Any known tier name (e.g. "cloud-code") selects that model directly."""
    m = (model or "").strip()
    if not m or m == "trilobite" or m.startswith("gpt-"):
        return None  # default: local student
    if m in server.TIERS:
        return m
    return None


def _run_prompt(prompt, history=None, tier=None):
    """Call the real trilobite loop with the UI's prior turns; returns UI text."""
    global LAST_IID, LAST_RESPONSE

    out = server.answer_with_history(prompt, history, trace=TRACE, strict=STRICT, tier=tier)
    if out.startswith("ERROR"):
        return out
    LAST_IID = server.parse_interaction_id(out)
    LAST_RESPONSE = out
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

    def _send_auth_error(self):
        body = json.dumps({
            "error": {"message": "invalid api key", "type": "auth"},
        }).encode("utf-8")
        self.send_response(401)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        _maybe_live_reload()
        if self.path.rstrip("/") == "/v1/models":
            if not check_auth(self.headers.get("Authorization", ""), API_KEY):
                self._send_auth_error()
                return
            # Advertise the local student plus every configured tier, so a client can
            # offer a model picker. owned_by flags where each runs.
            data = [{"id": "trilobite", "object": "model", "owned_by": "local"}]
            for tier_name, model in server.TIERS.items():
                data.append({
                    "id": tier_name,
                    "object": "model",
                    "owned_by": "cloud"
                    if server._is_cloud_tier(tier_name, model)
                    else "local",
                })
            body = json.dumps({"object": "list", "data": data}).encode("utf-8")
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path.rstrip("/") == "/v1/trilobite/status":
            if not check_auth(self.headers.get("Authorization", ""), API_KEY):
                self._send_auth_error()
                return
            payload = {
                "status": server.status(),
                "stats": server.trilobite_stats(),
                "learn_tiers": server.learn_tiers(),
                "models": [
                    {"id": "trilobite", "owned_by": "local"},
                    *[
                        {
                            "id": tier_name,
                            "owned_by": "cloud"
                            if server._is_cloud_tier(tier_name, model)
                            else "local",
                        }
                        for tier_name, model in server.TIERS.items()
                    ],
                ],
            }
            body = json.dumps(payload).encode("utf-8")
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
        _maybe_live_reload()
        if self.path.rstrip("/") != "/v1/chat/completions":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        if not check_auth(self.headers.get("Authorization", ""), API_KEY):
            self._send_auth_error()
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
        history = _history_from_messages(messages)

        try:
            reply = _handle_slash(prompt)
            if reply is None:
                reply = _handle_feedback(prompt)
            if reply is None:
                reply = _handle_intent(prompt)
            content = reply if reply is not None else _run_prompt(
                prompt, history, _model_to_tier(model))
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

    httpd = ThreadingHTTPServer((HOST, port), Handler)
    url = "http://%s:%d" % (HOST, port)
    print("trilobite_serve listening on %s" % url)
    print("auth: %s" % ("ON (api key required)" if API_KEY else "OFF (open, local use only)"))
    print("point your chat UI's OpenAI API base at %s/v1 (any api key)" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
