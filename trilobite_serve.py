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
import hmac
import hashlib
import ipaddress
import os
import sqlite3
import sys
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
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


def _env_int(name, default):
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_flag(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def _resolve_auth_mode(api_key="", require_account=False, configured=None):
    configured = os.environ.get("TRILOBITE_AUTH_MODE", "") if configured is None else configured
    mode = (configured or "").strip().lower().replace("_", "-")
    if mode:
        if mode not in ("api-key", "account", "both", "either"):
            raise RuntimeError("invalid TRILOBITE_AUTH_MODE")
        return mode
    if api_key:
        return "api-key"
    if require_account:
        return "account"
    return "local-open"


def _parse_cors_origins(value):
    return frozenset(
        origin.strip()
        for origin in (value or "").split(",")
        if origin.strip() and origin.strip() != "*"
    )

# Auth + bind config. No credentials remains open only on loopback.
API_KEY = os.environ.get("TRILOBITE_API_KEY", "")
HOST = os.environ.get("TRILOBITE_HOST", "127.0.0.1")
REQUIRE_ACCOUNT = _env_flag("TRILOBITE_REQUIRE_ACCOUNT")
AUTH_MODE = _resolve_auth_mode(API_KEY, REQUIRE_ACCOUNT)
CORS_ORIGINS = _parse_cors_origins(os.environ.get("TRILOBITE_CORS_ORIGINS", ""))
ALLOW_REGISTRATION = _env_flag("TRILOBITE_ALLOW_REGISTRATION")
MAX_REQUEST_BYTES = max(1, min(16 * 1024 * 1024, _env_int(
    "TRILOBITE_MAX_REQUEST_BYTES", 1024 * 1024
)))

# Server state (module globals, single-user local — mirrors trilobite_repl.py).
TRACE = False
STRICT = None  # None = env default (server._STRICT_DEFAULT)
LAST_IID = None
LAST_RESPONSE = None  # full last assistant turn (with footer), for /run
LAST_RUN_SOURCE = None  # answer-only text; trace/footer removed for /run
CURRENT_ACCOUNT = None
CURRENT_TOKEN = ""
CHAT_EVENTS = []


@dataclass
class ConversationState:
    """Mutable state for one hosted conversation, guarded by its lock."""

    trace: bool = False
    strict: object = None
    last_iid: str | None = None
    last_response: str | None = None
    last_run_source: str | None = None
    token: str = ""
    account: dict | None = None
    events: list = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)


@dataclass(frozen=True)
class TurnResult:
    content: str
    iid: str | None
    run_source: str


class _LegacyConversationState:
    """Adapter preserving direct helper/REPL-style module-global behavior."""

    lock = threading.RLock()

    @property
    def trace(self):
        return TRACE

    @trace.setter
    def trace(self, value):
        global TRACE
        TRACE = value

    @property
    def strict(self):
        return STRICT

    @strict.setter
    def strict(self, value):
        global STRICT
        STRICT = value

    @property
    def last_iid(self):
        return LAST_IID

    @last_iid.setter
    def last_iid(self, value):
        global LAST_IID
        LAST_IID = value

    @property
    def last_response(self):
        return LAST_RESPONSE

    @last_response.setter
    def last_response(self, value):
        global LAST_RESPONSE
        LAST_RESPONSE = value

    @property
    def last_run_source(self):
        return LAST_RUN_SOURCE

    @last_run_source.setter
    def last_run_source(self, value):
        global LAST_RUN_SOURCE
        LAST_RUN_SOURCE = value

    @property
    def token(self):
        return CURRENT_TOKEN

    @token.setter
    def token(self, value):
        global CURRENT_TOKEN
        CURRENT_TOKEN = value

    @property
    def account(self):
        return CURRENT_ACCOUNT

    @account.setter
    def account(self, value):
        global CURRENT_ACCOUNT
        CURRENT_ACCOUNT = value

    @property
    def events(self):
        return CHAT_EVENTS


_LEGACY_STATE = _LegacyConversationState()
HTTP_SESSION_STATE_LIMIT = max(1, min(
    1024, _env_int("TRILOBITE_HTTP_SESSION_STATE_LIMIT", 128)
))
_HTTP_SESSION_STATES = OrderedDict()
_HTTP_SESSION_STATES_LOCK = threading.RLock()


def _state_or_legacy(state):
    return state if state is not None else _LEGACY_STATE


def _state_principal(context):
    account = context.get("account") or {}
    if account:
        identity = account.get("username") or account.get("id") or "unknown"
        return "account:%s" % identity
    if context.get("api_key"):
        return "api-key"
    return "local-open"


def _prune_http_session_states(max_size=HTTP_SESSION_STATE_LIMIT):
    for key, candidate in list(_HTTP_SESSION_STATES.items()):
        if len(_HTTP_SESSION_STATES) <= max_size:
            break
        if candidate.lock.acquire(blocking=False):
            try:
                _HTTP_SESSION_STATES.pop(key, None)
            finally:
                candidate.lock.release()


def _http_conversation_state(context, session, token=""):
    """Return bounded per-principal state; a blank HTTP session is ephemeral."""

    session = (session or "").strip()
    if not session:
        return ConversationState(token=token or "", account=context.get("account"))
    key = (_state_principal(context), session)
    with _HTTP_SESSION_STATES_LOCK:
        state = _HTTP_SESSION_STATES.get(key)
        if state is None:
            _prune_http_session_states(HTTP_SESSION_STATE_LIMIT - 1)
            if len(_HTTP_SESSION_STATES) >= HTTP_SESSION_STATE_LIMIT:
                # All retained conversations are active. Stay bounded and use
                # request-local state rather than evicting an in-flight lock.
                return ConversationState(
                    token=token or "", account=context.get("account")
                )
            state = ConversationState()
            _HTTP_SESSION_STATES[key] = state
        _HTTP_SESSION_STATES.move_to_end(key)
        if token:
            state.token = token
        if context.get("account"):
            state.account = context["account"]
        return state


def _request_account_token(context, auth_header="", account_header=""):
    if not context.get("account"):
        return ""
    source = account_header or ("" if context.get("api_key") else auth_header)
    return _bearer_token(source)


def _http_scope_value(value, label):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise HTTPRequestError(400, "%s must be a string" % label)
    value = value.strip()
    if len(value) > 256:
        raise HTTPRequestError(400, "%s is too long" % label)
    return value


def _hosted_storage_id(context, value, kind):
    """Namespace durable HTTP state by principal without exposing client IDs."""
    value = _http_scope_value(value, kind)
    if not value or context.get("mode") == "local-open":
        return value
    material = "%s\0%s\0%s" % (kind, _state_principal(context), value)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]
    return "http-%s-%s" % (kind, digest)


TRAIN_DEFAULT_N = 3


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
    token = _bearer_token(auth_header)
    return hmac.compare_digest(token.encode("utf-8"), api_key.encode("utf-8"))


def _bearer_token(auth_header):
    auth_header = auth_header or ""
    if auth_header[:7].lower() == "bearer ":
        return auth_header[7:].strip()
    return auth_header.strip()


def _auth_account(auth_header):
    token = _bearer_token(auth_header)
    if not token or (API_KEY and hmac.compare_digest(token, API_KEY)):
        return None
    return server._admin_account_from_token(token)


def _effective_auth_mode():
    if AUTH_MODE == "local-open":
        if API_KEY:
            return "api-key"
        if REQUIRE_ACCOUNT:
            return "account"
    return AUTH_MODE


def _auth_context(auth_header="", account_header=""):
    mode = _effective_auth_mode()
    api_key_ok = bool(API_KEY) and check_auth(auth_header, API_KEY)
    account_source = account_header or ("" if api_key_ok else auth_header)
    account = _auth_account(account_source) if account_source else None
    authorized = {
        "api-key": api_key_ok,
        "account": account is not None,
        "both": api_key_ok and account is not None,
        "either": api_key_ok or account is not None,
        "local-open": True,
    }[mode]
    return {
        "mode": mode,
        "authorized": authorized,
        "api_key": api_key_ok,
        "account": account,
    }


def _authorized(auth_header, account_header=""):
    return _auth_context(auth_header, account_header)["authorized"]


def _developer_authorized(context):
    if context.get("mode") == "local-open":
        return True
    if not context["authorized"]:
        return False
    account = context.get("account")
    role_ok = bool(account) and account.get("role") in ("developer", "admin")
    if context["mode"] == "both":
        return role_ok
    return bool(context.get("api_key")) or role_ok


def _is_loopback_host(host):
    value = (host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _validate_bind_security(host, api_key=None, auth_mode=None, auth_secret=None):
    api_key = API_KEY if api_key is None else api_key
    mode = _effective_auth_mode() if auth_mode is None else auth_mode
    auth_secret = os.environ.get("TRILOBITE_AUTH_SECRET", "") if auth_secret is None else auth_secret
    if mode == "api-key" and not api_key:
        raise RuntimeError("api-key auth mode requires TRILOBITE_API_KEY")
    if mode == "both" and (not api_key or not auth_secret):
        raise RuntimeError("both auth mode requires API key and account auth secret")
    if _is_loopback_host(host):
        return
    strong_api = len(api_key) >= 24
    strong_account = len(auth_secret) >= 32
    secure = {
        "api-key": strong_api,
        "account": strong_account,
        "both": strong_api and strong_account,
        "either": strong_api and strong_account,
        "local-open": False,
    }.get(mode, False)
    if not secure:
        raise RuntimeError(
            "non-loopback bind requires explicitly configured strong authentication"
        )


DANGEROUS_HTTP_SLASH_COMMANDS = frozenset({
    "/dump", "/contextsize", "/ctxsize", "/permissions", "/perms",
    "/todo", "/task", "/tasks", "/qualityfix", "/emotion", "/emotions",
    "/vectors", "/mood", "/prefer", "/preference", "/preferences",
    "/register", "/admin", "/accounts", "/setaccount", "/debug", "/inspect",
    "/filepolicy", "/files", "/find", "/read", "/write", "/append", "/edit",
    "/delete", "/master", "/pass", "/good", "/accept", "/accepted", "/used",
    "/copied", "/edited", "/fail", "/bad", "/trace", "/strict", "/run",
    "/runwindow", "/runnew", "/runconsole", "/runproject", "/train", "/learn",
})


def _dangerous_http_slash(content):
    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return False
    return stripped.split(None, 1)[0].lower() in DANGEROUS_HTTP_SLASH_COMMANDS


class HTTPRequestError(Exception):
    def __init__(self, status, message, error_type="invalid_request"):
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_type = error_type


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


def _record_chat(role, content, kind="message", state=None):
    state = _state_or_legacy(state)
    state.events.append({
        "role": "%s/%s" % (role, kind),
        "content": content or "",
    })
    del state.events[:-200]


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
    return _do_run_from_messages(timeout=timeout, state=_LEGACY_STATE)


def _run_sources_from_messages(messages=None, state=None):
    state = _state_or_legacy(state)
    seen = set()
    for msg in reversed(messages or []):
        if msg.get("role") != "assistant":
            continue
        content = _answer_only(msg.get("content") or "")
        if content and content not in seen:
            seen.add(content)
            yield content
    for source in (state.last_run_source, state.last_response):
        if source and source not in seen:
            seen.add(source)
            yield source


def _do_run_from_messages(
    timeout=grounding.DEFAULT_TIMEOUT, messages=None, state=None
):
    block = None
    for source in _run_sources_from_messages(messages, state=state):
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


def _do_run_window_from_messages(
    timeout=grounding.DEFAULT_TIMEOUT, messages=None, state=None
):
    block = None
    for source in _run_sources_from_messages(messages, state=state):
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
    return _do_runproject_from_messages(timeout=timeout, state=_LEGACY_STATE)


def _do_runproject_from_messages(
    timeout=grounding.MAX_TIMEOUT, messages=None, state=None
):
    files = []
    for source in _run_sources_from_messages(messages, state=state):
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


def _dump_chat(messages=None, label="chat", state=None):
    state = _state_or_legacy(state)
    label = (label or "chat").strip() or "chat"
    sections = [
        ("trace", "on" if state.trace else "off"),
        ("strict", str(state.strict)),
        ("last interaction id", state.last_iid or "(none)"),
        ("last answer source", state.last_run_source or "(none)"),
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
        events=state.events,
    )
    return "dumped chat/debug log to %s" % path


def _handle_slash(content, messages=None, state=None):
    """Return response text if `content` is a recognized slash command, else None."""
    state = _state_or_legacy(state)

    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "/help":
        return HELP_TEXT
    if cmd == "/dump":
        return _dump_chat(
            messages=messages, label=arg.strip() or "chat", state=state
        )
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
            state.token = out.split(marker, 1)[1].strip().splitlines()[0]
            state.account = server._admin_account_from_token(state.token)
        return out
    if cmd == "/whoami":
        return server.admin_whoami(state.token)
    if cmd == "/admin":
        return server.admin_status(state.token)
    if cmd == "/accounts":
        return server.admin_accounts(state.token)
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
            token=state.token,
            username=username,
            role=kv.get("role", ""),
            tier=kv.get("tier", ""),
            dev_flags=kv.get("dev_flags", ""),
            banned=kv.get("banned", ""),
        )
    if cmd in ("/debug", "/inspect"):
        return server.debug_inspect(state.token)
    if cmd in ("/cot", "/chainofthought", "/thoughts"):
        return server.admin_private_chain_of_thought(state.token)
    if cmd == "/filepolicy":
        return server.file_policy(token=state.token)
    if cmd in ("/files", "/find"):
        return server.file_find(query=arg.strip() or "*", token=state.token)
    if cmd == "/read":
        return server.file_read(path=arg.strip(), token=state.token)
    if cmd in ("/write", "/append"):
        parts2 = arg.split(None, 1)
        if len(parts2) != 2:
            return "usage: %s <path> <text>" % cmd
        return server.file_write(
            path=parts2[0],
            content=parts2[1],
            mode="append" if cmd == "/append" else "create",
            token=state.token,
        )
    if cmd == "/edit":
        pieces = arg.split("|", 2)
        if len(pieces) != 3:
            return "usage: /edit <path>|<old>|<new>"
        return server.file_edit(
            path=pieces[0].strip(),
            old=pieces[1],
            new=pieces[2],
            token=state.token,
        )
    if cmd == "/delete":
        return server.file_delete(
            path=arg.strip(), dry_run=True, token=state.token
        )
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
        if state.last_iid:
            msg = server.record_outcome(state.last_iid, "tests_passed")
            state.last_iid = None
            return msg
        return "(nothing to record yet)"
    if cmd in ("/accept", "/accepted", "/used", "/copied", "/edited"):
        if state.last_iid:
            signal = {
                "/accept": "accepted",
                "/accepted": "accepted",
                "/used": "used",
                "/copied": "copied",
                "/edited": "edited",
            }[cmd]
            msg = server.record_outcome(state.last_iid, signal)
            state.last_iid = None
            return msg
        return "(nothing to record yet)"
    if cmd in ("/fail", "/bad"):
        if state.last_iid:
            msg = server.record_outcome(state.last_iid, "failed")
            state.last_iid = None
            return msg
        return "(nothing to record yet)"
    if cmd == "/trace":
        state.trace = _on_off(arg, state.trace)
        return "trace %s" % ("on" if state.trace else "off")
    if cmd == "/strict":
        state.strict = _on_off(arg, state.strict)
        return "strict %s" % ("on" if state.strict else "off")
    if cmd == "/run":
        timeout, err = _parse_run_timeout(arg)
        if err:
            return err
        return _do_run_from_messages(timeout, messages=messages, state=state)
    if cmd in ("/runwindow", "/runnew", "/runconsole"):
        timeout, err = _parse_run_timeout(arg)
        if err:
            return err
        return _do_run_window_from_messages(
            timeout, messages=messages, state=state
        )
    if cmd == "/runproject":
        timeout, err = _parse_run_timeout(arg)
        if err:
            return err
        return _do_runproject_from_messages(
            timeout, messages=messages, state=state
        )
    if cmd in ("/train", "/learn"):
        n, err = _parse_train_n(arg)
        if err:
            return err
        return _do_train(n)

    return None  # not a recognized slash command — fall through to the model


def _handle_feedback(content, state=None):
    """Passive learning: if `content` reads as plain feedback on the last turn
    ("thanks, that worked" / "no that's wrong") rather than a new task, record
    the outcome on this conversation and return an acknowledgement. Else None (fall
    through to intent/model handling)."""
    state = _state_or_legacy(state)

    if not state.last_iid:
        return None

    signal = feedback.classify_signal(content)
    if signal and server.reward.score(signal) > 0:
        server.record_outcome(state.last_iid, signal)
        state.last_iid = None
        return "Got it - recorded %s so I can learn." % signal
    if signal:
        server.record_outcome(state.last_iid, signal)
        state.last_iid = None
        return "Got it - recorded that as not-helpful so I can learn."

    fb = feedback.classify_feedback(content)
    if fb == "positive":
        server.record_outcome(state.last_iid, "accepted")
        state.last_iid = None
        return "Got it — recorded that as helpful so I can learn."
    if fb == "negative":
        server.record_outcome(state.last_iid, "rejected")
        state.last_iid = None
        return "Got it — recorded that as not-helpful so I can learn."
    return None


def _handle_intent(content, messages=None, state=None):
    """Return response text if `content` is a natural-language control intent, else None."""
    state = _state_or_legacy(state)

    intent = intents.classify(content)
    if not intent:
        return None

    replies = []
    if "trace" in intent:
        state.trace = intent["trace"]
        replies.append("trace %s" % ("on" if state.trace else "off"))
    if "strict" in intent:
        state.strict = intent["strict"]
        replies.append("strict %s" % ("on" if state.strict else "off"))
    if intent.get("run"):
        replies.append(_do_run_from_messages(messages=messages, state=state))
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
    state=None, return_result=False,
):
    """Call the real trilobite loop with the UI's prior turns; returns UI text."""
    state = _state_or_legacy(state)

    out = server.answer_with_history(
        prompt, history, trace=state.trace, strict=state.strict, tier=tier,
        context_size=context_size, session=session, project=project,
    )
    if out.startswith("ERROR"):
        result = TurnResult(out, None, "")
        return result if return_result else result.content
    iid = server.parse_interaction_id(out)
    run_source = _answer_only(out)
    state.last_iid = iid
    state.last_response = out
    state.last_run_source = run_source
    result = TurnResult(_strip_footer(out), iid, run_source)
    return result if return_result else result.content


def _chat_completion_object(content, model="trilobite", iid=None):
    iid = iid or uuid.uuid4().hex[:12]
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
        origin = self.headers.get("Origin")
        if origin is not None and origin in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Trilobite-Account-Token, "
                "X-Trilobite-Bootstrap-Secret",
            )

    def log_message(self, fmt, *args):
        sys.stderr.write("[trilobite_serve] %s\n" % (fmt % args))

    def do_OPTIONS(self):
        if self._reject_disallowed_origin():
            return
        self.send_response(204)
        self._cors()
        self.end_headers()

    def _reject_disallowed_origin(self):
        origin = self.headers.get("Origin")
        if origin is None or origin in CORS_ORIGINS:
            return False
        self._send_json_payload(
            {"error": {"message": "origin is not allowed", "type": "cors"}},
            status=403,
        )
        return True

    def _request_auth_context(self):
        return _auth_context(
            self.headers.get("Authorization", ""),
            self.headers.get("X-Trilobite-Account-Token", ""),
        )

    def _send_auth_error(self):
        self._send_json_payload({
            "error": {"message": "authentication required", "type": "auth"},
        }, status=401)

    def _send_json_payload(self, payload, status=200):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        if self.headers.get("Transfer-Encoding"):
            raise HTTPRequestError(400, "transfer encoding is not supported")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise HTTPRequestError(411, "Content-Length is required")
        if not raw_length.strip().isdigit():
            raise HTTPRequestError(400, "Content-Length must be a nonnegative integer")
        length = int(raw_length)
        if length > MAX_REQUEST_BYTES:
            raise HTTPRequestError(413, "request body is too large")
        media_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise HTTPRequestError(415, "Content-Type must be application/json")
        raw = self.rfile.read(length)
        if len(raw) != length:
            raise HTTPRequestError(400, "request body is incomplete")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise HTTPRequestError(400, "request body must contain valid JSON")
        if not isinstance(payload, dict):
            raise HTTPRequestError(400, "request JSON must be an object")
        return payload

    def do_GET(self):
        if self._reject_disallowed_origin():
            return
        _maybe_live_reload()
        if self.path.rstrip("/") == "/v1/models":
            context = self._request_auth_context()
            if not context["authorized"]:
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
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            account = context["account"]
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
        self._send_json_payload(
            {"error": {"message": "not found", "type": "not_found"}}, status=404
        )

    def do_POST(self):
        if self._reject_disallowed_origin():
            return
        _maybe_live_reload()
        path = self.path.rstrip("/")
        try:
            req = self._read_json()
        except HTTPRequestError as error:
            self._send_json_payload(
                {"error": {"message": error.message, "type": error.error_type}},
                status=error.status,
            )
            return
        context = self._request_auth_context()
        if path == "/v1/trilobite/register":
            conn = server._open_db()
            try:
                account = admin_auth.register(
                    conn,
                    req.get("username", ""),
                    req.get("password", ""),
                    trusted_local=False,
                    bootstrap_secret=self.headers.get(
                        "X-Trilobite-Bootstrap-Secret", ""
                    ),
                    allow_additional=ALLOW_REGISTRATION,
                    actor=context["account"] if context["authorized"] else None,
                )
                self._send_json_payload({"ok": True, "account": account}, status=201)
            except PermissionError as error:
                self._send_json_payload({"ok": False, "message": str(error)}, status=403)
            except sqlite3.IntegrityError:
                self._send_json_payload({"ok": False, "message": "account already exists"}, status=409)
            except ValueError as error:
                self._send_json_payload({"ok": False, "message": str(error)}, status=400)
            except Exception as error:
                self.log_error("registration failed: %s", type(error).__name__)
                self._send_json_payload({"ok": False, "message": "registration failed"}, status=500)
            finally:
                conn.close()
            return
        if path == "/v1/trilobite/login":
            if context["mode"] in ("api-key", "both") and not context["api_key"]:
                self._send_auth_error()
                return
            conn = server._open_db()
            try:
                token, account = admin_auth.login(
                    conn, req.get("username", ""), req.get("password", "")
                )
                self._send_json_payload({"ok": True, "token": token, "account": account})
            except (ValueError, PermissionError):
                self._send_json_payload(
                    {"ok": False, "message": "invalid username or password"}, status=401
                )
            except Exception as error:
                self.log_error("login failed: %s", type(error).__name__)
                self._send_json_payload({"ok": False, "message": "login failed"}, status=500)
            finally:
                conn.close()
            return
        if path == "/v1/trilobite/admin/account":
            if not context["authorized"]:
                self._send_auth_error()
                return
            account = context["account"]
            ok, msg = admin_auth.require(account, "admin")
            if not ok:
                self._send_json_payload({"ok": False, "message": msg}, status=403)
                return
            account_header = self.headers.get("X-Trilobite-Account-Token", "")
            out = server.admin_set_account(
                token=_bearer_token(account_header or self.headers.get("Authorization", "")),
                username=req.get("username", ""),
                role=req.get("role", ""),
                tier=req.get("tier", ""),
                dev_flags=req.get("dev_flags", ""),
                banned=str(req.get("banned", "")),
            )
            self._send_json_payload({"ok": not out.startswith("ERROR:"), "message": out})
            return
        if path != "/v1/chat/completions":
            self._send_json_payload(
                {"error": {"message": "not found", "type": "not_found"}}, status=404
            )
            return

        if not context["authorized"]:
            self._send_auth_error()
            return
        account = context["account"]
        messages = req.get("messages", [])
        prompt = _last_user_message(messages)
        if _dangerous_http_slash(prompt) and not _developer_authorized(context):
            self._send_json_payload(
                {
                    "error": {
                        "message": "developer or admin authentication is required",
                        "type": "forbidden_command",
                    }
                },
                status=403,
            )
            return
        conn = server._open_db()
        try:
            ok, msg = admin_auth.rate_limit(conn, account)
        finally:
            conn.close()
        if not ok:
            self._send_json_payload({"error": {"message": msg, "type": "rate_limit"}}, status=429)
            return

        stream = bool(req.get("stream", False))
        model = req.get("model", "trilobite")
        context_size = req.get("context_size", "")
        try:
            session = _http_scope_value(req.get("session", ""), "session")
            project = _http_scope_value(req.get("project", ""), "project")
            storage_session = _hosted_storage_id(context, session, "session")
            storage_project = _hosted_storage_id(context, project, "project")
        except HTTPRequestError as error:
            self._send_json_payload(
                {"error": {"message": error.message, "type": error.error_type}},
                status=error.status,
            )
            return
        history = _history_from_messages(messages)
        account_header = self.headers.get("X-Trilobite-Account-Token", "")
        auth_header = self.headers.get("Authorization", "")
        state = _http_conversation_state(
            context,
            session,
            token=_request_account_token(context, auth_header, account_header),
        )

        reply = None
        turn = None
        response_iid = None
        try:
            with state.lock:
                _record_chat("user", prompt, state=state)
                with server.activity_tracker.response_span(
                    "chat:%s" % (model or "trilobite"),
                    prompt,
                    surface="http",
                    model=model,
                    session=storage_session,
                    project=storage_project,
                ):
                    reply = _handle_slash(
                        prompt, messages=messages, state=state
                    )
                    if reply is None:
                        reply = _handle_feedback(prompt, state=state)
                    if reply is None and _developer_authorized(context):
                        reply = _handle_intent(
                            prompt, messages=messages, state=state
                        )
                    if reply is not None:
                        content = reply
                    else:
                        turn = _run_prompt(
                            prompt,
                            history,
                            _model_to_tier(model),
                            context_size=context_size,
                            session=storage_session,
                            project=storage_project,
                            state=state,
                            return_result=True,
                        )
                        content = turn.content
                        response_iid = turn.iid
                    if "=== ACTIVITY (observable work) ===" not in content:
                        content = server._append_activity(content)
                _record_chat(
                    "assistant",
                    content,
                    kind="slash" if reply is not None else "model",
                    state=state,
                )
        except Exception as error:
            self.log_error("request failed: %s", type(error).__name__)
            self._send_json_payload(
                {"error": {"message": "internal server error", "type": "server_error"}},
                status=500,
            )
            return

        if stream:
            self._send_stream(content, model, iid=response_iid)
        else:
            self._send_json(
                _chat_completion_object(content, model, iid=response_iid)
            )

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

    def _send_stream(self, content, model, iid=None):
        iid = iid or uuid.uuid4().hex[:12]
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

    _validate_bind_security(HOST)
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    url = "http://%s:%d" % (HOST, port)
    print("trilobite_serve listening on %s" % url)
    print("auth mode: %s" % _effective_auth_mode())
    print("point your chat UI's OpenAI API base at %s/v1" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
