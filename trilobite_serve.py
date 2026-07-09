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
import admin_auth
import grounding
import code_runner
import training_tasks
import intents
import feedback
import live_reload
import debug_dump

DEFAULT_PORT = 11435

# Auth + bind config. Empty API_KEY = auth disabled (local-only default).
API_KEY = os.environ.get("TRILOBITE_API_KEY", "")
HOST = os.environ.get("TRILOBITE_HOST", "127.0.0.1")
REQUIRE_ACCOUNT = os.environ.get("TRILOBITE_REQUIRE_ACCOUNT", "").strip().lower() in (
    "1", "true", "yes", "on"
)

# Server state (module globals, single-user local — mirrors trilobite_repl.py).
TRACE = False
STRICT = None  # None = env default (server._STRICT_DEFAULT)
LAST_IID = None
LAST_RESPONSE = None  # full last assistant turn (with footer), for /run
LAST_RUN_SOURCE = None  # answer-only text; trace/footer removed for /run
CURRENT_ACCOUNT = None
CURRENT_TOKEN = ""
CHAT_EVENTS = []

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
    "admin_auth",
    "command_registry",
    "permission_rules",
    "debug_dump",
]

HELP_TEXT = """commands:
  /help              show this help
  /trace [on|off]    toggle trace mode (bare = on); shows retrieval + prompt
  /strict [on|off]   toggle strict mode (bare = on); pins to the trilobite alias
  /stats             show trilobite's learning stats
  /context           show context, session, and memory health meters
  /contextsize [N]   show/set requested context (8k..1m; native num_ctx is clamped)
  /compact           preview context compaction/rollover recommendations
  /commands [filter] list available commands by category, name, or risk
  /activity          show active/latest tool calls and file changes
  /dump [label]      dump chat log and debug info to a text file
  /todo ...          list/add/update visible task state
  /quality           audit lesson quality and duplicate rows
  /qualityfix [apply] dry-run or apply exact duplicate lesson cleanup
  /emotion [cmd]     show/tune live tone vectors; try: /emotion tune warmer shorter
  /prefer [text]     show/teach preferences; /prefer forget <id-or-key>
  /improve           show the next system improvement checklist
  /master [mode] ... run master orchestration: ask, inline, or delegate
  /agents            show live master/subagent activity
  /register u p      create account (first account becomes admin)
  /login u p         login for admin/debug commands
  /whoami            show current account
  /admin             show admin status
  /accounts          list accounts (admin)
  /setaccount ...    admin account edits: user role= tier= dev_flags= banned=
  /debug             inspect safe debug state
  /cot               denied: hidden private chain-of-thought is not exposed
  /permissions [tool] show local permission rules or one matched rule
  /filepolicy        show file access roots and bypass controls
  /files [query]     find files under guarded roots
  /read <path>       read a guarded file
  /write <p> <text>  create a guarded file
  /append <p> <text> append to a guarded file
  /edit <p>|<old>|<new> replace text in a guarded file
  /delete <path>     dry-run delete; output shows required confirm string
  /pass, /good       record the last answer as tests_passed
  /accept,/used      record the last answer as accepted/used
  /copied,/edited    record copy/edit passive learning signals
  /fail, /bad        record the last answer as failed
  /run [seconds]     execute the code block from the last response (default 8s)
  /runwindow [sec]   launch the last code block in a separate Windows console
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


def _bearer_token(auth_header):
    auth_header = auth_header or ""
    if auth_header.startswith("Bearer "):
        return auth_header.split(None, 1)[1].strip()
    return auth_header.strip()


def _auth_account(auth_header):
    token = _bearer_token(auth_header)
    if not token or token == API_KEY:
        return None
    return server._admin_account_from_token(token)


def _authorized(auth_header):
    account = _auth_account(auth_header)
    if REQUIRE_ACCOUNT:
        return account is not None or (bool(API_KEY) and check_auth(auth_header, API_KEY))
    if check_auth(auth_header, API_KEY):
        return True
    return account is not None or not API_KEY


def _strip_footer(text):
    idx = text.find(server.FOOTER_PREFIX)
    if idx == -1:
        return text
    return text[:idx]


def _strip_trace(text):
    marker = "\n=== TRACE (how trilobite decided) ==="
    idx = (text or "").find(marker)
    if idx == -1:
        idx = (text or "").find("=== TRACE (how trilobite decided) ===")
    if idx == -1:
        return text or ""
    return (text or "")[:idx].rstrip()


def _answer_only(text):
    return _strip_trace(_strip_footer(text or "")).rstrip()


def _record_chat(role, content, kind="message"):
    CHAT_EVENTS.append({
        "role": "%s/%s" % (role, kind),
        "content": content or "",
    })
    del CHAT_EVENTS[:-200]


def _maybe_live_reload():
    global server, grounding, training_tasks, intents, feedback, admin_auth, debug_dump
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    server = modules.get("server", server)
    grounding = modules.get("grounding", grounding)
    training_tasks = modules.get("training_tasks", training_tasks)
    intents = modules.get("intents", intents)
    feedback = modules.get("feedback", feedback)
    admin_auth = modules.get("admin_auth", admin_auth)
    debug_dump = modules.get("debug_dump", debug_dump)


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
    return _do_run_from_messages(timeout=timeout)


def _run_sources_from_messages(messages=None):
    seen = set()
    for source in (LAST_RUN_SOURCE, LAST_RESPONSE):
        if source and source not in seen:
            seen.add(source)
            yield source
    for msg in reversed(messages or []):
        if msg.get("role") != "assistant":
            continue
        content = _answer_only(msg.get("content") or "")
        if content and content not in seen:
            seen.add(content)
            yield content


def _do_run_from_messages(timeout=grounding.DEFAULT_TIMEOUT, messages=None):
    block = None
    for source in _run_sources_from_messages(messages):
        block = grounding.extract_runnable_code_block(source)
        if block is not None:
            break
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


def _do_run_window_from_messages(timeout=grounding.DEFAULT_TIMEOUT, messages=None):
    block = None
    for source in _run_sources_from_messages(messages):
        block = grounding.extract_runnable_code_block(source)
        if block is not None:
            break
    if block is None:
        return "(no code block in the last response to run)"
    result = code_runner.run_code_window(
        block["code"],
        language=block["language"],
        timeout=timeout,
    )
    status = "[launched]" if result.get("ok") else "[launch failed]"
    return "%s\n%s" % (code_runner.format_window_result(result), status)


def _do_runproject(timeout=grounding.MAX_TIMEOUT):
    return _do_runproject_from_messages(timeout=timeout)


def _do_runproject_from_messages(timeout=grounding.MAX_TIMEOUT, messages=None):
    files = []
    for source in _run_sources_from_messages(messages):
        files = grounding.extract_project_files(source)
        if files:
            break
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


def _dump_chat(messages=None, label="chat"):
    label = (label or "chat").strip() or "chat"
    sections = [
        ("trace", "on" if TRACE else "off"),
        ("strict", str(STRICT)),
        ("last interaction id", LAST_IID or "(none)"),
        ("last answer source", LAST_RUN_SOURCE or "(none)"),
        ("context", server.context_health()),
        ("quality", server.memory_quality_report(sample_limit=5)),
        ("agents", server.master_status(limit=20)),
        ("diagnostics", server.diagnostics()),
    ]
    path = debug_dump.write_dump(
        server.trilobite_paths.default_home(),
        label=label,
        messages=messages or [],
        sections=sections,
        events=CHAT_EVENTS,
    )
    return "dumped chat/debug log to %s" % path


def _handle_slash(content, messages=None):
    """Return response text if `content` is a recognized slash command, else None."""
    global TRACE, STRICT, LAST_IID, CURRENT_ACCOUNT, CURRENT_TOKEN

    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return HELP_TEXT
    if cmd == "/dump":
        return _dump_chat(messages=messages, label=arg.strip() or "chat")
    if cmd == "/stats":
        return server.trilobite_stats()
    if cmd == "/context":
        return server.context_health()
    if cmd in ("/contextsize", "/ctxsize"):
        if arg.strip():
            return server.set_context_size(arg.strip())
        return server.context_policy_status()
    if cmd in ("/compact", "/compaction"):
        return server.context_compaction_plan()
    if cmd in ("/commands", "/cmds"):
        return server.command_registry_list(arg.strip())
    if cmd in ("/permissions", "/perms"):
        return server.permission_policy(arg.strip())
    if cmd in ("/todo", "/task", "/tasks"):
        text = arg.strip()
        if not text or text.lower() in ("list", "ls"):
            return server.task_list()
        action, _, rest = text.partition(" ")
        action = action.lower()
        if action in ("add", "create", "new"):
            return server.task_create(title=rest.strip())
        if action in ("done", "complete", "finish"):
            if not rest.strip():
                return "usage: /todo done <task-id>"
            return server.task_update(task_id=rest.strip(), status="done")
        if action in ("start", "doing"):
            if not rest.strip():
                return "usage: /todo start <task-id>"
            return server.task_update(task_id=rest.strip(), status="in_progress")
        if action in ("block", "blocked"):
            if not rest.strip():
                return "usage: /todo block <task-id>"
            return server.task_update(task_id=rest.strip(), status="blocked")
        if action in ("show", "view"):
            if not rest.strip():
                return "usage: /todo show <task-id>"
            return server.task_show(rest.strip())
        return (
            "usage: /todo [list] | /todo add <title> | /todo start <id> | "
            "/todo done <id> | /todo block <id> | /todo show <id>"
        )
    if cmd == "/quality":
        return server.memory_quality_report()
    if cmd == "/qualityfix":
        return server.memory_quality_repair(apply=(arg.strip().lower() == "apply"))
    if cmd in ("/emotion", "/emotions", "/vectors", "/mood"):
        return server.emotion_command(arg)
    if cmd in ("/prefer", "/preference", "/preferences"):
        return server.preference_command(arg)
    if cmd in ("/improve", "/improvements"):
        return server.system_improvement_report()
    if cmd in ("/agents", "/masterstatus"):
        return server.master_status()
    if cmd in ("/activity", "/tools", "/work"):
        return server.activity_status()
    if cmd == "/register":
        parts2 = arg.split(None, 1)
        if len(parts2) != 2:
            return "usage: /register <username> <password>"
        return server.admin_register(parts2[0], parts2[1])
    if cmd == "/login":
        parts2 = arg.split(None, 1)
        if len(parts2) != 2:
            return "usage: /login <username> <password>"
        out = server.admin_login(parts2[0], parts2[1])
        marker = "token: "
        if marker in out and not out.startswith("ERROR:"):
            CURRENT_TOKEN = out.split(marker, 1)[1].strip().splitlines()[0]
            CURRENT_ACCOUNT = server._admin_account_from_token(CURRENT_TOKEN)
        return out
    if cmd == "/whoami":
        return server.admin_whoami(CURRENT_TOKEN)
    if cmd == "/admin":
        return server.admin_status(CURRENT_TOKEN)
    if cmd == "/accounts":
        return server.admin_accounts(CURRENT_TOKEN)
    if cmd == "/setaccount":
        parts2 = arg.split()
        if not parts2:
            return "usage: /setaccount <username> role=developer tier=pro dev_flags=x banned=false"
        username = parts2[0]
        kv = {}
        for item in parts2[1:]:
            if "=" in item:
                k, v = item.split("=", 1)
                kv[k] = v
        return server.admin_set_account(
            token=CURRENT_TOKEN,
            username=username,
            role=kv.get("role", ""),
            tier=kv.get("tier", ""),
            dev_flags=kv.get("dev_flags", ""),
            banned=kv.get("banned", ""),
        )
    if cmd in ("/debug", "/inspect"):
        return server.debug_inspect(CURRENT_TOKEN)
    if cmd in ("/cot", "/chainofthought", "/thoughts"):
        return server.admin_private_chain_of_thought(CURRENT_TOKEN)
    if cmd == "/filepolicy":
        return server.file_policy(token=CURRENT_TOKEN)
    if cmd in ("/files", "/find"):
        return server.file_find(query=arg.strip() or "*", token=CURRENT_TOKEN)
    if cmd == "/read":
        return server.file_read(path=arg.strip(), token=CURRENT_TOKEN)
    if cmd in ("/write", "/append"):
        parts2 = arg.split(None, 1)
        if len(parts2) != 2:
            return "usage: %s <path> <text>" % cmd
        return server.file_write(
            path=parts2[0],
            content=parts2[1],
            mode="append" if cmd == "/append" else "create",
            token=CURRENT_TOKEN,
        )
    if cmd == "/edit":
        pieces = arg.split("|", 2)
        if len(pieces) != 3:
            return "usage: /edit <path>|<old>|<new>"
        return server.file_edit(
            path=pieces[0].strip(),
            old=pieces[1],
            new=pieces[2],
            token=CURRENT_TOKEN,
        )
    if cmd == "/delete":
        return server.file_delete(path=arg.strip(), dry_run=True, token=CURRENT_TOKEN)
    if cmd == "/master":
        text = arg.strip()
        mode = "ask"
        task = text
        if text:
            parts = text.split(None, 1)
            if parts[0].lower() in ("ask", "inline", "master", "delegate", "delegated", "agents", "parallel"):
                mode = parts[0].lower()
                task = parts[1] if len(parts) > 1 else ""
        return server.master_orchestrate(task=task, mode=mode)
    if cmd in ("/pass", "/good"):
        if LAST_IID:
            msg = server.record_outcome(LAST_IID, "tests_passed")
            LAST_IID = None
            return msg
        return "(nothing to record yet)"
    if cmd in ("/accept", "/accepted", "/used", "/copied", "/edited"):
        if LAST_IID:
            signal = {
                "/accept": "accepted",
                "/accepted": "accepted",
                "/used": "used",
                "/copied": "copied",
                "/edited": "edited",
            }[cmd]
            msg = server.record_outcome(LAST_IID, signal)
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
        return _do_run_from_messages(timeout, messages=messages)
    if cmd in ("/runwindow", "/runnew", "/runconsole"):
        timeout, err = _parse_run_timeout(arg)
        if err:
            return err
        return _do_run_window_from_messages(timeout, messages=messages)
    if cmd == "/runproject":
        timeout, err = _parse_run_timeout(arg)
        if err:
            return err
        return _do_runproject_from_messages(timeout, messages=messages)
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

    signal = feedback.classify_signal(content)
    if signal and server.reward.score(signal) > 0:
        server.record_outcome(LAST_IID, signal)
        LAST_IID = None
        return "Got it - recorded %s so I can learn." % signal
    if signal:
        server.record_outcome(LAST_IID, signal)
        LAST_IID = None
        return "Got it - recorded that as not-helpful so I can learn."

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


def _run_prompt(
    prompt, history=None, tier=None, context_size="", session="", project="",
):
    """Call the real trilobite loop with the UI's prior turns; returns UI text."""
    global LAST_IID, LAST_RESPONSE, LAST_RUN_SOURCE

    out = server.answer_with_history(
        prompt, history, trace=TRACE, strict=STRICT, tier=tier,
        context_size=context_size, session=session, project=project,
    )
    if out.startswith("ERROR"):
        return out
    LAST_IID = server.parse_interaction_id(out)
    LAST_RESPONSE = out
    LAST_RUN_SOURCE = _answer_only(out)
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
        "trilobite_activity": server.activity_tracker.snapshot().get("latest"),
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

    def _send_json_payload(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def do_GET(self):
        _maybe_live_reload()
        if self.path.rstrip("/") == "/v1/models":
            if not _authorized(self.headers.get("Authorization", "")):
                self._send_auth_error()
                return
            # Advertise the local student plus every configured tier, so a client can
            # offer a model picker. owned_by flags where each runs.
            data = [{"id": "trilobite", "object": "model", "owned_by": "local"}]
            for tier_name, model in server.available_tiers().items():
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
            auth_header = self.headers.get("Authorization", "")
            if not _authorized(auth_header):
                self._send_auth_error()
                return
            account = _auth_account(auth_header)
            payload = {
                "status": server.status(),
                "stats": server.trilobite_stats(),
                "learn_tiers": server.learn_tiers(),
                "improvements": server.system_improvement_report(),
                "context": server.context_health_data(),
                "context_policy": server.context_policy.policy(server.SESSION_NUM_CTX),
                "agents": server.master_orchestrator.snapshot(),
                "activity": server.activity_tracker.snapshot(),
                "db_path": getattr(server, "_DB_PATH", ""),
                "state_home": str(server.trilobite_paths.default_home()),
                "account": account or {},
                "models": [
                    {"id": "trilobite", "owned_by": "local"},
                    *[
                        {
                            "id": tier_name,
                            "owned_by": "cloud"
                            if server._is_cloud_tier(tier_name, model)
                            else "local",
                        }
                        for tier_name, model in server.available_tiers().items()
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
        path = self.path.rstrip("/")
        if path == "/v1/trilobite/register":
            try:
                req = self._read_json()
                out = server.admin_register(req.get("username", ""), req.get("password", ""))
                self._send_json_payload({"ok": not out.startswith("ERROR:"), "message": out})
            except Exception as e:
                self._send_json_payload({"ok": False, "message": str(e)}, status=400)
            return
        if path == "/v1/trilobite/login":
            try:
                req = self._read_json()
                out = server.admin_login(req.get("username", ""), req.get("password", ""))
                if out.startswith("ERROR:"):
                    self._send_json_payload({"ok": False, "message": out}, status=401)
                    return
                token = out.split("token: ", 1)[1].strip().splitlines()[0]
                account = server._admin_account_from_token(token)
                self._send_json_payload({"ok": True, "token": token, "account": account})
            except Exception as e:
                self._send_json_payload({"ok": False, "message": str(e)}, status=400)
            return
        if path == "/v1/trilobite/admin/account":
            auth_header = self.headers.get("Authorization", "")
            account = _auth_account(auth_header)
            ok, msg = admin_auth.require(account, "admin")
            if not ok:
                self._send_json_payload({"ok": False, "message": msg}, status=403)
                return
            req = self._read_json()
            out = server.admin_set_account(
                token=_bearer_token(auth_header),
                username=req.get("username", ""),
                role=req.get("role", ""),
                tier=req.get("tier", ""),
                dev_flags=req.get("dev_flags", ""),
                banned=str(req.get("banned", "")),
            )
            self._send_json_payload({"ok": not out.startswith("ERROR:"), "message": out})
            return
        if path != "/v1/chat/completions":
            self.send_response(404)
            self._cors()
            self.end_headers()
            return

        auth_header = self.headers.get("Authorization", "")
        if not _authorized(auth_header):
            self._send_auth_error()
            return
        account = _auth_account(auth_header)
        conn = server._open_db()
        try:
            ok, msg = admin_auth.rate_limit(conn, account)
        finally:
            conn.close()
        if not ok:
            self._send_json_payload({"error": {"message": msg, "type": "rate_limit"}}, status=429)
            return

        try:
            req = self._read_json()
        except Exception as e:
            self._send_error_completion("ERROR parsing request body: %s" % e, stream=False)
            return

        messages = req.get("messages", [])
        stream = bool(req.get("stream", False))
        model = req.get("model", "trilobite")
        context_size = req.get("context_size", "")
        session = req.get("session", "")
        project = req.get("project", "")
        prompt = _last_user_message(messages)
        history = _history_from_messages(messages)
        _record_chat("user", prompt)

        reply = None
        try:
            with server.activity_tracker.response_span(
                "chat:%s" % (model or "trilobite"),
                prompt,
                surface="http",
                model=model,
                session=session,
                project=project,
            ):
                reply = _handle_slash(prompt, messages=messages)
                if reply is None:
                    reply = _handle_feedback(prompt)
                if reply is None:
                    reply = _handle_intent(prompt)
                content = reply if reply is not None else _run_prompt(
                    prompt, history, _model_to_tier(model),
                    context_size=context_size, session=session, project=project)
                if "=== ACTIVITY (observable work) ===" not in content:
                    content = server._append_activity(content)
        except Exception:
            content = "ERROR: %s" % traceback.format_exc()
        _record_chat("assistant", content, kind="slash" if reply is not None else "model")

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
