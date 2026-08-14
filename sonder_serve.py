"""sonder_serve — OpenAI-compatible HTTP proxy in front of Sonder Runtime's learning loop.

Lets any OpenAI-compatible chat UI (Open WebUI, etc.) talk to server.sonder()
instead of raw Ollama, including the REPL's slash-command powers (/stats, /pass,
/fail, /trace, /strict). Stdlib only (http.server / json / urllib) — zero-dep,
matching the rest of this project.

Run:
    python sonder_serve.py [port]
    (or set env SONDER_PORT; default 11435)

Point your chat UI's OpenAI API base at http://127.0.0.1:<port>/v1 (any api key).
"""
import json
import hmac
import hashlib
import ipaddress
import math
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import permission_modes
import server
import command_catalog
import admin_auth
import sonder_config
import grounding
import code_runner
import training_tasks
import intents
import feedback
import live_reload
import debug_dump
import sonder_health
import sonder_lifecycle
import sonder_secrets
import tool_contract
import unsafe_lab

DEFAULT_PORT = 11435
_LOCAL_LOG_TAIL_BYTES = 64 * 1024
_LOCAL_LOG_SECRET = re.compile(
    r"(?i)\b(authorization|api[_-]?key|token|password|credential)\s*[:=]\s*(?:bearer\s+)?\S+"
)


def _local_server_log_tail():
    """Return a bounded, redacted tail for the loopback-only diagnostics page."""
    path = Path(server.sonder_paths.default_home()) / "run" / "sonder_serve.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _LOCAL_LOG_TAIL_BYTES))
            raw = stream.read(_LOCAL_LOG_TAIL_BYTES)
    except OSError:
        return "(server log is not available yet)"
    text = raw.decode("utf-8", errors="replace")
    if size > len(raw):
        text = "(showing the latest %d KiB)\n%s" % (_LOCAL_LOG_TAIL_BYTES // 1024, text)
    return _LOCAL_LOG_SECRET.sub(r"\1=<redacted>", text)


_LOCAL_LOG_PAGE = """<!doctype html>
<meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Sonder local server</title>
<style>body{margin:0;background:#07121f;color:#d9efff;font:14px ui-monospace,Consolas,monospace}header{padding:16px 22px;background:#0b2b4a;color:#75cfff;font-weight:700}main{padding:16px 22px}small{color:#9cb4c8}pre{white-space:pre-wrap;word-break:break-word;background:#050b12;border:1px solid #16466e;padding:14px;min-height:60vh}</style>
<header>Sonder local server <small id=\"state\">connecting</small></header><main><p>Live, read-only server-log tail. This page is available only from loopback.</p><pre id=\"log\">Loading…</pre></main>
<script>const log=document.getElementById('log'),state=document.getElementById('state');async function refresh(){try{const r=await fetch('/v1/local/server-log',{cache:'no-store'});const j=await r.json();log.textContent=j.log||'';state.textContent='updated '+new Date().toLocaleTimeString()}catch(e){state.textContent='feed unavailable'}}refresh();setInterval(refresh,1000)</script>"""


def _request_route(path) -> str:
    """Return a normalized routing path while preserving query data elsewhere."""
    return urllib.parse.urlsplit(str(path or "")).path.rstrip("/") or "/"


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
    configured = os.environ.get("SONDER_AUTH_MODE", "") if configured is None else configured
    mode = (configured or "").strip().lower().replace("_", "-")
    if mode:
        if mode not in ("api-key", "account", "both", "either"):
            raise RuntimeError("invalid SONDER_AUTH_MODE")
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
API_KEY = os.environ.get("SONDER_API_KEY", "")
HOST = os.environ.get("SONDER_HOST", "127.0.0.1")
REQUIRE_ACCOUNT = _env_flag("SONDER_REQUIRE_ACCOUNT")
AUTH_MODE = _resolve_auth_mode(API_KEY, REQUIRE_ACCOUNT)
CORS_ORIGINS = _parse_cors_origins(os.environ.get("SONDER_CORS_ORIGINS", ""))
ALLOW_REGISTRATION = _env_flag("SONDER_ALLOW_REGISTRATION")
MAX_REQUEST_BYTES = max(1, min(16 * 1024 * 1024, _env_int(
    "SONDER_MAX_REQUEST_BYTES", 1024 * 1024
)))
# Per-connection socket timeout. StreamRequestHandler applies one only when the
# handler declares it, and Handler declared none: every read blocked forever, so
# a client that connected and never finished its request line parked in
# rfile.readline() for good. ThreadingHTTPServer had already given it a dedicated
# thread, and every real defence -- origin check, auth rate limit, the 413 body
# cap, the bounded-concurrency admission slot -- sits DOWNSTREAM of the headers
# that never arrived, so no credential was needed to hold a thread. This bounds
# the wait; it does not fire during generation, because a long model call
# performs no socket operation while it runs.
REQUEST_TIMEOUT_SECONDS = max(5, _env_int("SONDER_REQUEST_TIMEOUT_SECONDS", 60))
# Account-secret counterpart to sonder_config.MIN_API_KEY_LENGTH. That side of
# the same non-loopback bind policy has no constant anywhere, so name it here
# rather than leave a second bare literal next to the one being removed.
MIN_ACCOUNT_SECRET_LENGTH = 32
LAUNCHER_HEALTH_TOKEN = os.environ.get(sonder_health.TOKEN_ENV, "")
RUNTIME_ROLE = os.environ.get(sonder_health.ROLE_ENV, "")

# Server state (module globals, single-user local — mirrors sonder_repl.py).
BOUND_PORT = None  # set by main() once the listener is actually bound
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
    # Model reasoning for this turn, when the deployment exposes it. Empty
    # otherwise, which is the default.
    thinking: str = ""
    # The target recorded here is reported by server.py at the point it is
    # actually selected for generation.  Do not derive this from the caller's
    # OpenAI ``model`` field: aliases and dynamic catalogs can make that value
    # misleading.
    resolved_model: str = ""
    resolved_tier: str = ""


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
    1024, _env_int("SONDER_HTTP_SESSION_STATE_LIMIT", 128)
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


def _task_account_scope(context):
    """Return an opaque durable task boundary for an authenticated account.

    Local-open and direct MCP remain deliberately global.  Account names must
    not become task-store data: callers can choose them, and retaining a
    domain-separated digest gives the store only a stable authorization key.
    """
    account = (context or {}).get("account") or {}
    if not account:
        return None
    material = "task-account-scope\0" + _state_principal(context)
    return "ta-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


_SCOPED_TASK_TOOLS = frozenset((
    "task_create", "task_list", "task_update", "task_show", "task_delete",
    "task_plan", "task_progress", "task_depend", "checklist_create",
    "checklist_show", "checklist_update",
))

# These tools may create or update checklists *inside* server.py after the
# HTTP dispatcher has handed off control.  The public MCP variants deliberately
# remain global for a local operator, but an account-backed HTTP request has an
# opaque task boundary and must never silently escape it.  There is not yet a
# first-class task-scope argument on the agent/master/workflow internals, so
# fail closed here rather than create durable state in the shared namespace.
_ACCOUNT_UNSCOPED_TASK_TOOLS = frozenset((
    "agent", "workbench_agent", "master_orchestrate", "master_retry",
    "agent_retry", "self_heal_repair",
))
_ACCOUNT_UNSCOPED_SAVED_WORKFLOW_TOOLS = frozenset((
    # Saved workflows live in one operator-owned workflows.json.  Letting a
    # hosted account list it exposes another account's action payloads, and
    # save/delete/run would let one account overwrite, remove, or execute
    # another account's durable automation.  Do not pretend the file is
    # account-scoped until the repository has a first-class owner boundary.
    "workflow_list", "workflow_save", "workflow_delete", "workflow_run",
))
_ACCOUNT_UNSCOPED_LOOP_ACTIONS = frozenset((
    "checklist_create", "checklist_update", "checklist_show",
    "work", "agent", "workbench_agent", "master", "master_orchestrate",
    # Retrying an existing master re-enters the worker path, which enables
    # auto_checklist.  It is therefore the same global task-store escape as
    # a fresh master run, even though the retry itself names no task row.
    "master_retry", "agent_retry", "self_heal_repair",
))


def _account_task_boundary_refusal(tool_name, kwargs, context):
    """Reject account requests whose internal task writes cannot be scoped.

    This is intentionally an HTTP-only choke point.  Direct MCP and
    local-open operation are the single-user/operator surface and retain their
    historical global task/checklist behavior.  A malformed loop payload is
    left to ``server.loop`` so callers still receive its useful JSON contract;
    only a recognizable action that can touch the global task namespace is
    refused here.
    """
    if _task_account_scope(context) is None:
        return ""
    name = str(tool_name or "").strip().lower()
    if name in _ACCOUNT_UNSCOPED_TASK_TOOLS:
        return (
            "refused /%s: account-scoped task state is not available for "
            "this autonomous tool; use the scoped task/checklist commands."
        ) % name
    if name in _ACCOUNT_UNSCOPED_SAVED_WORKFLOW_TOOLS:
        return (
            "refused /%s: account-scoped saved workflows are not available; "
            "use an operator-managed local workflow session."
        ) % name
    if name != "loop":
        return ""
    try:
        parsed = json.loads((kwargs or {}).get("actions_json", ""))
    except (TypeError, ValueError):
        return ""
    actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
    if not isinstance(actions, list):
        return ""
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or action.get("action") or "").strip().lower()
        if action_type in _ACCOUNT_UNSCOPED_LOOP_ACTIONS:
            return (
                "refused /loop: account-scoped task state is not available "
                "for loop action '%s'; use scoped task/checklist commands."
            ) % action_type
    return ""


def _served_task_tool(tool_name, kwargs, context):
    """Run account-scoped task operations without exposing a scope argument."""
    scope = _task_account_scope(context)
    if scope is not None:
        return server.scoped_task_tool_dispatch(tool_name, kwargs, account_scope=scope)
    handler = getattr(server, tool_name)
    return str(handler(**kwargs))


def _fanout_request_owner(context):
    """Stable, non-secret receipt owner key for durable fanout state.

    Usernames are client-controlled account data and may look like a secret
    assignment (for example ``api_key=...``).  Receipt storage redacts text by
    design, so never use that raw principal as an authorization key.  A domain
    separated digest remains stable for owner comparison without persisting an
    identifier that the generic secret redactor can transform or collide.
    """
    material = "fanout-owner\0" + _state_principal(context)
    return "fo-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _fanout_request_role(context):
    account = context.get("account") or {}
    role = str(account.get("role") or "").strip()
    if role:
        return role
    return "api-key" if context.get("api_key") else "local-open"


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


TRAIN_MAX_N = max(1, _env_int("SONDER_TRAIN_MAX_N", 500))

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
    "tool_contract",
    "debug_dump",
]


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
    api_key_ok = bool(API_KEY) and (
        check_auth(auth_header, API_KEY)
        # Rotation overlap: the previous key's hash is accepted until its
        # mandatory expiry (SPEC-2 section 5).
        or sonder_secrets.previous_key_valid(_bearer_token(auth_header))
    )
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


def _execution_feed_detail_allowed(context):
    """Evidence needs both the exact local flag and developer authority."""
    return bool(
        server.activity_tracker.detail_enabled()
        and context.get("mode") != "local-open"
        and _developer_authorized(context)
    )


def _admin_authorized(context):
    """Administrator authorization for privileged operations (SPEC-2).

    In account-bearing modes the account must hold the admin role; in
    api-key-only deployments the single owner key is the administrator
    credential.
    """
    if context.get("mode") == "local-open":
        return True
    if not context.get("authorized"):
        return False
    account = context.get("account")
    if account is not None:
        ok, _ = admin_auth.require(account, "admin")
        return ok
    if context.get("mode") == "both":
        return False
    return bool(context.get("api_key"))


# Operations that alter shared runtime authority or persist privileged state.
# Keep this at the HTTP boundary: hiding a command in the app does not stop a
# crafted request, and a prompt must never be the thing that confers a role.
SYSTEM_OPERATION_ROLES = {
    "permission_mode_change": "admin",
    "runtime_policy_change": "admin",
    "permission_rule_change": "admin",
    "account_management": "admin",
    "selfmod_deploy": "admin",
    "automation_lifecycle": "developer",
    "workspace_execution": "developer",
}

# The catalogued ``/<tool>`` surface resolves tool names dynamically, so it
# cannot safely rely on a hand-maintained branch list.  Bind the small set of
# shared-authority tools to the operation they perform at that one choke point.
# Direct local MCP/console use remains a trusted operator surface; this map is
# deliberately enforced only when an HTTP request supplies an auth context.
SYSTEM_OPERATION_TOOLS = {
    "permission_mode": "permission_mode_change",
    "permission_rule_set": "permission_rule_change",
    "elevate": "permission_mode_change",
    "runtime_policy_update": "runtime_policy_change",
    "runtime_source_update": "selfmod_deploy",
    # These mutate process-wide runtime behaviour or durable prompt inputs.
    # They must not be reachable by ordinary served accounts through their
    # registered ``/<tool>`` names when the curated aliases are gated.
    "set_context_size": "runtime_policy_change",
    "unload": "runtime_policy_change",
    "update_emotion_vectors": "runtime_policy_change",
    "tune_emotion_vectors": "runtime_policy_change",
    "learn_preference": "runtime_policy_change",
    "update_system_profile": "selfmod_deploy",
    "self_heal_repair": "selfmod_deploy",
    "admin_set_account": "account_management",
    # Read-only, but it lists every hosted account: the tool's own
    # `_admin_require(token, "admin")` was the only thing between an ordinary
    # served account and the account roster. The boundary owns the role
    # decision; the in-tool check remains as the second lock.
    "admin_accounts": "account_management",
    "autopilot_start": "automation_lifecycle",
    "autopilot_resume": "automation_lifecycle",
    "autopilot_pause": "automation_lifecycle",
    "autopilot_cancel": "automation_lifecycle",
    "workflow_save": "automation_lifecycle",
    "workflow_delete": "automation_lifecycle",
    # A saved workflow can contain any loop action, including the shared
    # controls above.  Treat its execution as a runtime-policy operation;
    # otherwise a developer can save or invoke an admin-authored payload to
    # bypass the per-action HTTP gate.
    "workflow_run": "runtime_policy_change",
    "memory_export": "workspace_execution",
    "memory_privacy_repair": "workspace_execution",
    "memory_quality_repair": "workspace_execution",
}


def _system_operation_authority_error(operation, context):
    """Return a role-boundary refusal, or "" when this caller may proceed."""
    required = SYSTEM_OPERATION_ROLES.get(str(operation or ""))
    if required == "admin" and not _admin_authorized(context):
        return "administrator authorization is required"
    if required == "developer" and not _developer_authorized(context):
        return "developer or administrator authorization is required"
    return ""


def _is_loopback_host(host):
    value = (host or "").strip().strip("[]").lower()
    if value == "localhost":
        return True
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


def _validate_bind_security(host, api_key=None, auth_mode=None, auth_secret=None):
    # Unsafe lab acknowledgement tightens exposure: unlike normal served mode,
    # there is deliberately no authenticated non-loopback topology available.
    unsafe_lab.require_startup(host=host)
    api_key = API_KEY if api_key is None else api_key
    mode = _effective_auth_mode() if auth_mode is None else auth_mode
    auth_secret = os.environ.get("SONDER_AUTH_SECRET", "") if auth_secret is None else auth_secret
    if mode == "api-key" and not api_key:
        raise RuntimeError("api-key auth mode requires SONDER_API_KEY")
    if mode == "both" and (not api_key or not auth_secret):
        raise RuntimeError("both auth mode requires API key and account auth secret")
    if _is_loopback_host(host):
        return
    # The same policy sonder_config.validate enforces, read from the same
    # constant. It was restated here as a bare 24, so raising the named
    # MIN_API_KEY_LENGTH -- the obvious single-point edit -- would have
    # hardened the config validator and left the actual bind-time gate behind.
    strong_api = len(api_key) >= sonder_config.MIN_API_KEY_LENGTH
    strong_account = len(auth_secret) >= MIN_ACCOUNT_SECRET_LENGTH
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
    # Belongs beside /debug rather than apart from it. It was omitted while it
    # could only ever refuse; an opted-in deployment now returns a turn's
    # reasoning through it, and an omission justified by a refusal must not
    # outlive the refusal.
    "/cot", "/chainofthought", "/thoughts",
    "/filepolicy", "/files", "/find", "/read", "/write", "/append", "/edit",
    "/delete", "/master", "/pass", "/good", "/accept", "/accepted", "/used",
    "/copied", "/edited", "/fail", "/bad", "/trace", "/strict", "/run",
    "/runwindow", "/runnew", "/runconsole", "/runproject", "/train", "/learn",
    "/asset", "/assets", "/assetgen", "/artifact", "/forge", "/gamesuite",
    "/game", "/gamegen", "/gamefleet", "/gamecampaign",
    "/activity", "/tools", "/work", "/agent", "/report", "/endreport",
    "/checklist", "/plan", "/inventory", "/workspace", "/tree", "/folders", "/search", "/grep",
    "/programs", "/programfind", "/scripts", "/scriptfind", "/image",
    "/inspectimage", "/mkdir", "/runprogram", "/runscript",
    "/privacy", "/privacyreview", "/privacyfix", "/embeddings", "/embedfix",
    "/capacity", "/agentcapacity", "/agentcancel", "/cancelagents",
    "/agentretry", "/retryagent",
    "/runtime", "/models",
    "/update", "/updatecheck", "/updatesource",
    "/selfmod", "/selfmodify",
    "/elevate",
    # Spends several full model load+generate cycles per call.
    "/ensemble", "/council",
})


def _dangerous_http_slash(content):
    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return False
    pieces = stripped.split(None, 2)
    command = pieces[0].lower()
    if command in ("/autopilot", "/auto"):
        action = pieces[1].lower() if len(pieces) > 1 else "status"
        return action not in ("status", "show", "list", "help", "?")
    if command in ("/runtime", "/models"):
        action = pieces[1].lower() if len(pieces) > 1 else "status"
        return action not in ("status", "show", "list", "help", "?")
    if command in ("/update", "/updatecheck", "/updatesource"):
        if command == "/updatecheck":
            return False
        action = pieces[1].lower() if len(pieces) > 1 else "apply"
        return action in ("apply", "now")
    if command in ("/selfmod", "/selfmodify"):
        action = pieces[1].lower() if len(pieces) > 1 else "status"
        return action not in ("status", "show", "list", "history", "inspect", "diff", "tests", "backups", "verify-backup", "opportunities", "help", "?")
    return command in DANGEROUS_HTTP_SLASH_COMMANDS


# Slash names gated by action rather than by membership in the frozenset above.
_CONDITIONALLY_GATED_SLASH = frozenset({
    "/autopilot", "/auto", "/runtime", "/models", "/update", "/updatecheck", "/updatesource", "/selfmod", "/selfmodify",
})

# Who clears _developer_authorized() in each mode. Keep in step with it.
_DEVELOPER_AUTHORITY_BY_MODE = {
    "local-open": "every caller (unauthenticated)",
    "api-key": "the owner API key, or a developer/admin account",
    "account": "an authenticated account, and it must hold developer or admin",
    "both": "the owner API key AND an account holding developer or admin",
    "either": "the owner API key, or an account holding developer or admin",
}


def _deployment_gating_summary():
    """Report which HTTP permission tier this deployment is actually running in.

    The tiers themselves are enforced by _dangerous_http_slash() and
    _developer_authorized(); this only makes the effective mode visible, which
    is otherwise only observable by getting refused.
    """
    mode = _effective_auth_mode()
    gated = DANGEROUS_HTTP_SLASH_COMMANDS | _CONDITIONALLY_GATED_SLASH
    lines = [
        "Deployment gating (HTTP surface)",
        "  effective auth mode : %s" % mode,
        "  developer authority : %s" % _DEVELOPER_AUTHORITY_BY_MODE.get(mode, "unknown"),
        "  gated slash names   : %d (aliases included)" % len(gated),
        "  model reasoning     : %s" % (
            "exposed to %s" % _reasoning_audience()
            if server.reasoning_exposure_enabled()
            else "not exposed (SONDER_EXPOSE_REASONING is off)"
        ),
        "  bind                : %s:%s%s" % (
            HOST,
            DEFAULT_PORT if BOUND_PORT is None else BOUND_PORT,
            "" if _is_loopback_host(HOST) else "  (non-loopback)",
        ),
    ]
    if mode == "local-open":
        lines.append(
            "  note                : anyone who can reach this port holds developer\n"
            "                        authority. That is the intended local default, and a\n"
            "                        non-loopback bind is refused outright in this mode.\n"
            "                        To restrict: set SONDER_API_KEY, or SONDER_REQUIRE_ACCOUNT=1\n"
            "                        and grant developer/admin per account."
        )
    else:
        lines.append(
            "  note                : callers without developer authority are refused the\n"
            "                        gated names above; everything else stays available."
        )
    return "\n".join(lines)


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
    marker = "\n=== TRACE (how Sonder Runtime decided) ==="
    idx = (text or "").find(marker)
    if idx == -1:
        idx = (text or "").find("=== TRACE (how Sonder Runtime decided) ===")
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
    global server, grounding, training_tasks, intents, feedback, admin_auth, debug_dump, tool_contract
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    server = modules.get("server", server)
    grounding = modules.get("grounding", grounding)
    training_tasks = modules.get("training_tasks", training_tasks)
    intents = modules.get("intents", intents)
    feedback = modules.get("feedback", feedback)
    admin_auth = modules.get("admin_auth", admin_auth)
    debug_dump = modules.get("debug_dump", debug_dump)
    tool_contract = modules.get("tool_contract", tool_contract)


def _on_off(arg, current):
    arg = (arg or "").strip().lower()
    if arg in ("", "on"):
        return True
    if arg == "off":
        return False
    return current


_SUPPORTED_CHAT_ROLES = frozenset({"system", "user", "assistant"})


def _validate_chat_messages(messages):
    """Validate the supported OpenAI-compatible chat message subset.

    Sonder currently accepts text-only system, user, and assistant messages.
    Reject unsupported multimodal/tool shapes at the HTTP boundary so malformed
    requests receive a structured 400 instead of reaching history helpers.
    """
    if not isinstance(messages, list):
        raise HTTPRequestError(400, "messages must be an array")

    last_user_content = None
    for index, message in enumerate(messages):
        if not isinstance(message, dict):
            raise HTTPRequestError(
                400, "messages[%d] must be an object" % index,
            )
        if "role" not in message:
            raise HTTPRequestError(
                400, "messages[%d].role is required" % index,
            )
        role = message["role"]
        if not isinstance(role, str) or role not in _SUPPORTED_CHAT_ROLES:
            raise HTTPRequestError(
                400,
                "messages[%d].role must be one of: assistant, system, user"
                % index,
            )
        if "content" not in message:
            raise HTTPRequestError(
                400, "messages[%d].content is required" % index,
            )
        content = message["content"]
        if not isinstance(content, str):
            raise HTTPRequestError(
                400,
                "messages[%d].content must be a string; multimodal content is not supported"
                % index,
            )
        if role == "user":
            last_user_content = content

    if last_user_content is None or not last_user_content.strip():
        raise HTTPRequestError(
            400, "messages must contain a non-empty user message",
        )
    return messages


def _last_user_message(messages):
    for msg in reversed(messages or []):
        if msg.get("role") == "user":
            return msg.get("content") or ""
    return ""


def _history_from_messages(messages):
    """Prior user/assistant turns from the UI request, excluding the current (last
    user) message. A full chat UI owns conversation state here, so we thread
    exactly what it sends rather than a DB session; thin clients that name a
    session without resending a transcript fall back to
    _server_side_history()."""
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


SERVER_SIDE_HISTORY_TURNS = 8


def _server_side_history(storage_session, limit=SERVER_SIDE_HISTORY_TURNS):
    """Rebuild prior turns from the stored session for thin clients.

    The OpenAI-compatible contract is client-owned history, but a client
    that names a `session` while sending only the current message clearly
    expects the server to remember. Feed the stored turns back through the
    same history channel so both contracts work; activity footers are
    stripped so replayed context stays clean.
    """
    if not (storage_session or "").strip():
        return []
    try:
        import sonder_runtime.adapters.memory_store as memory_store

        session_id = server._resolve_session(storage_session)
        conn = server._open_db()
        try:
            turns = memory_store.session_turns(conn, session_id)
        finally:
            conn.close()
    except Exception:
        return []
    history = []
    for turn in turns[-max(1, int(limit)):]:
        task = (turn.get("task") or "").strip()
        response = (turn.get("response") or "").split("=== ACTIVITY")[0]
        response = _strip_footer(response).strip()
        if task:
            history.append({"role": "user", "content": task})
        if response:
            history.append({"role": "assistant", "content": response})
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
    """Run grounded practice over n tasks; this records lessons, not weight updates."""
    tasks = training_tasks.sample(n)
    passed = 0
    lessons = 0
    lines = []
    for t in tasks:
        lines.append("  practicing: %s ..." % t["name"])
        resp = server.sonder(t["prompt"])
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
    lines.append("practiced %d tasks: %d passed, %d failed, %d new lessons" % (
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
        server.sonder_paths.default_home(),
        label=label,
        messages=messages or [],
        sections=sections,
        events=state.events,
    )
    return "dumped chat/debug log to %s" % path


def _http_slash_refusal(cmd, argument="", context=None):
    """The permission gate for this chain: "" to proceed, else the refusal text.

    This chain calls `server.file_write` / `file_edit` / `file_delete`
    directly and forwards ten more names to `server.control_command`, and
    until now nothing in front of it consulted `permission_modes` --
    `permission_modes` was imported here only to *set* the mode at
    `/v1/permission-mode`. So a shipped `file_delete: deny` rule bound at four
    surfaces and not at this one, and `plan`, which this same surface both
    selects and displays on its mode chip, did not hold still here.

    `_dangerous_http_slash` + `_developer_authorized` is not this check. That
    pair is an authentication tier -- it asks *who* is calling, and in the
    default `local-open` deployment the answer is "anyone who can reach this
    port" (see `_deployment_gating_summary`). It has nothing to say about
    which mode is in force or which rules an operator wrote.

    `interactive=False`, like every other caller with nobody to prompt: `ask`
    degrades to `allow`, so this surface refuses nothing today that it did not
    refuse before, while a `deny` rule and `plan` refuse. Only a `deny` can
    come back from `decide()` here, which is why this is a flat loop rather
    than a copy of the console's ask-and-rank gate.

    The gate's own control is exempt for the reason it is everywhere else --
    though the app's real way back out of `plan` is the `/v1/permission-mode`
    endpoint, which is not a slash command and is not gated.

    This covers the `cmd ==` chain only. The chain is the curated slice; the
    catalogued fall-through *after* it reaches any registered tool by its own
    name and is gated separately -- see `_dispatch_catalogued_tool`.
    """
    try:
        tools = command_catalog.http_slash_tools().get(cmd, ())
    except command_catalog.CatalogUnavailable as exc:
        # Fail closed on ignorance: an empty map would answer "allowed" for
        # every command in this chain.
        return "refused %s: %s" % (cmd, exc)
    # The catalog deliberately reports the union of a slash alias' branches.
    # These aliases contain both an inspection and a process-wide mutation, so
    # applying the mutation's admin gate to `/emotion status` or `/prefer
    # status` would accidentally turn a read-only request into an admin-only
    # operation.  Narrow only after the command's own grammar has established
    # that it will take the read-only branch.
    read_only_argument = str(argument or "").strip().lower()
    if cmd in ("/emotion", "/emotions", "/vectors", "/mood") and (
        not read_only_argument or read_only_argument in ("status", "list", "show")
    ):
        tools = ("emotion_vector_status",)
    elif cmd in ("/prefer", "/preference", "/preferences") and (
        not read_only_argument or read_only_argument in ("status", "list", "show")
    ):
        tools = ("preferences_status",)
    elif cmd in ("/contextsize", "/ctxsize") and not read_only_argument:
        tools = ("context_policy_status",)
    elif cmd in ("/runtime", "/models") and (
        not read_only_argument or read_only_argument == "status"
    ):
        tools = ("runtime_policy_status",)
    return _http_tool_refusal(tools, cmd, context=context)


def _loop_global_operation_refusal(actions_json, context=None):
    """Refuse HTTP loop payloads that would mutate shared runtime state.

    ``server.loop`` remains the single parser/executor for validity and
    bounded-loop semantics.  This shallow inspection only recognizes global
    actions early enough to apply served-account authority; malformed
    payloads continue to receive the loop tool's normal error.

    Derived, not hand-kept: each action resolves to its canonical tool
    through ``server._loop_action_tool`` -- the same resolution the loop's
    own permission gate uses -- and then meets exactly the role binding its
    ``/<tool>`` spelling meets (``tool_contract.system_operation_for``,
    deny-by-default for a declared system operation with no binding). The
    hand map this replaces named 4 of the 7 loop-reachable system
    operations, so ``{"type": "memory_privacy_repair"}`` ran for an
    ordinary served account while ``/memory_privacy_repair`` required the
    developer role -- the same work, the other spelling.
    """
    if context is None:
        return ""
    try:
        parsed = json.loads(actions_json)
    except (TypeError, ValueError):
        return ""
    actions = parsed.get("actions") if isinstance(parsed, dict) else parsed
    if not isinstance(actions, list):
        return ""
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = str(action.get("type") or action.get("action") or "").strip().lower()
        operation = tool_contract.system_operation_for(
            server._loop_action_tool(action_type)
        )
        if not operation:
            continue
        if operation == tool_contract.SYSTEM_OPERATION_UNBOUND:
            if not _admin_authorized(context):
                return (
                    "refused /loop: administrator authorization is required "
                    "for an unclassified system operation"
                )
            continue
        authority_error = _system_operation_authority_error(operation, context)
        if authority_error:
            return "refused /loop: %s" % authority_error
    return ""


def _http_tool_refusal(tools, label, context=None):
    """The decision itself, shared by this surface's two entry points.

    Only a `deny` can come back under `interactive=False`, so this is a flat
    loop rather than a copy of the console's ask-and-rank gate.

    The operation lookup goes through `tool_contract.system_operation_for`
    rather than reading `SYSTEM_OPERATION_TOOLS` directly, which buys two
    things: alias spellings canonicalize before grading, and a tool the
    runtime declares to be a system operation (`_AGENT_SYSTEM_OPERATOR_TOOLS`)
    that nobody bound to a role fails CLOSED for served accounts instead of
    open. `admin_accounts` was the live case: agent-refused, catalogued
    `ask`, no binding -- an ordinary served account reached the tool body and
    only its in-tool token check stood between a crafted request and the
    account list. Boundary drift must not depend on every tool author
    remembering a second map.
    """
    for tool in tools:
        operation = tool_contract.system_operation_for(tool)
        if operation and context is not None:
            if operation == tool_contract.SYSTEM_OPERATION_UNBOUND:
                # Durable-authority tools are already refused non-interactively
                # by decide() below -- on every surface, for every role -- and
                # its refusal names the remedy (the console prompt or an
                # explicit allow rule). Returning the generic unbound message
                # here would replace actionable text with a role demand that
                # even an admin cannot satisfy on this path.
                if str(tool or "").lstrip("/") in permission_modes.DURABLE_AUTHORITY_TOOLS:
                    pass
                elif not _admin_authorized(context):
                    return (
                        "refused %s: administrator authorization is required "
                        "for an unclassified system operation" % label
                    )
            else:
                authority_error = _system_operation_authority_error(operation, context)
                if authority_error:
                    return "refused %s: %s" % (label, authority_error)
        decision = permission_modes.decide_for_caller(
            tool, interactive=False, gate_control_exempt=True,
        )
        if decision is None:
            continue
        if decision.action == permission_modes.DENY:
            return "refused %s: %s (mode: %s)" % (
                label, decision.reason,
                permission_modes.MODE_LABELS.get(decision.mode, decision.mode),
            )
    return ""


def _slash_system_operation(command, argument):
    """Classify named slash controls that bypass catalogued tool dispatch.

    ``/runtime set`` is parsed by ``server.control_command`` rather than the
    dynamic ``/<tool>`` catalog.  Keeping this tiny parser next to the HTTP
    choke point prevents a role declaration from becoming decorative merely
    because a command has two spellings.
    """
    command = str(command or "").strip().lower()
    parts = str(argument or "").strip().split(None, 1)
    action = parts[0].lower() if parts else ""
    if command in ("/runtime", "/models") and action in ("set", "reset"):
        return "runtime_policy_change"
    if command in ("/update", "/updatesource") and action in ("", "apply", "now"):
        return "selfmod_deploy"
    return ""


def _handle_slash(content, messages=None, state=None, project="", context=None):
    """Return response text if `content` is a recognized slash command, else None."""
    state = _state_or_legacy(state)

    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    # One choke point in front of every branch below, for the same reason the
    # REPL has one: this is a flat chain of ~130 `if cmd == ...` returns, and a
    # check placed after even one of them leaves that one ungated.
    refusal = _http_slash_refusal(cmd, arg, context=context)
    if refusal:
        return refusal
    operation = _slash_system_operation(cmd, arg)
    if operation and context is not None:
        authority_error = _system_operation_authority_error(operation, context)
        if authority_error:
            return "refused %s: %s" % (cmd, authority_error)

    if cmd == "/help":
        # The catalog derives every command from the dispatch chains and the
        # live tool registry, so /help cannot drift behind them the way the
        # hand-written text it replaced did.
        return command_catalog.help_text(arg.strip())
    if cmd == "/dump":
        return _dump_chat(
            messages=messages, label=arg.strip() or "chat", state=state
        )
    if cmd == "/stats":
        return server.sonder_stats()
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
    if cmd in ("/ensemble", "/council"):
        text = arg.strip()
        if not text:
            return (
                "usage: /ensemble <question>\n"
                "       /ensemble [tiers=a,b] <question>\n"
                "Asks each bound local model the question, then compounds one\n"
                "answer. Sequential by design -- only one model fits on the card."
            )
        tiers = ""
        if text.lower().startswith("tiers="):
            head, _, rest = text.partition(" ")
            tiers = head.split("=", 1)[1]
            text = rest.strip()
        if not text:
            return "usage: /ensemble [tiers=a,b] <question>"
        return server.ensemble_answer(text, tiers=tiers)
    if cmd in ("/permissions", "/perms"):
        policy = server.permission_policy(arg.strip())
        return "%s\n\n%s" % (policy, _deployment_gating_summary())
    if cmd in ("/todo", "/task", "/tasks"):
        text = arg.strip()
        if not text or text.lower() in ("list", "ls"):
            return _served_task_tool("task_list", {}, context)
        action, _, rest = text.partition(" ")
        action = action.lower()
        if action in ("add", "create", "new"):
            return _served_task_tool("task_create", {"title": rest.strip()}, context)
        if action in ("done", "complete", "finish"):
            if not rest.strip():
                return "usage: /todo done <task-id>"
            return _served_task_tool("task_update", {"task_id": rest.strip(), "status": "done"}, context)
        if action in ("start", "doing"):
            if not rest.strip():
                return "usage: /todo start <task-id>"
            return _served_task_tool("task_update", {"task_id": rest.strip(), "status": "in_progress"}, context)
        if action in ("block", "blocked"):
            if not rest.strip():
                return "usage: /todo block <task-id>"
            return _served_task_tool("task_update", {"task_id": rest.strip(), "status": "blocked"}, context)
        if action in ("show", "view"):
            if not rest.strip():
                return "usage: /todo show <task-id>"
            return _served_task_tool("task_show", {"task_id": rest.strip()}, context)
        return (
            "usage: /todo [list] | /todo add <title> | /todo start <id> | "
            "/todo done <id> | /todo block <id> | /todo show <id>"
        )
    if cmd == "/quality":
        return server.memory_quality_report()
    if cmd == "/qualityfix":
        return server.memory_quality_repair(apply=(arg.strip().lower() == "apply"))
    if cmd in ("/privacy", "/privacyreview", "/privacyfix", "/embeddings", "/embedfix"):
        return server.control_command(stripped, project=project)
    if cmd in ("/emotion", "/emotions", "/vectors", "/mood"):
        return server.emotion_command(arg)
    if cmd in ("/prefer", "/preference", "/preferences"):
        return server.preference_command(arg)
    if cmd in ("/improve", "/improvements"):
        return server.system_improvement_report()
    if cmd in ("/agents", "/masterstatus"):
        return server.master_status()
    if cmd in (
        "/capacity", "/agentcapacity", "/agentcancel", "/cancelagents",
        "/agentretry", "/retryagent",
    ):
        if cmd in ("/agentretry", "/retryagent"):
            task_boundary_error = _account_task_boundary_refusal(
                "master_retry", {}, context,
            )
            if task_boundary_error:
                return task_boundary_error
        return server.control_command(stripped, project=project)
    if cmd in ("/activity", "/tools"):
        return server.activity_status()
    if cmd in ("/autopilot", "/auto"):
        return server.control_command(stripped, project=project)
    if cmd in ("/runtime", "/models"):
        return server.control_command(stripped, project=project)
    if cmd in ("/update", "/updatecheck", "/updatesource"):
        return server.control_command(stripped, project=project)
    if cmd in ("/selfmod", "/selfmodify"):
        return server.control_command(stripped, project=project)
    if cmd in ("/goal", "/goals"):
        return server.control_command(stripped, project=project)
    if cmd in ("/mcp", "/convergence"):
        return server.control_command(stripped, project=project)
    if cmd in ("/learning", "/learnhealth", "/metrics"):
        return server.control_command(stripped, project=project)
    if cmd in ("/weather", "/forecast"):
        if not arg.strip():
            return "usage: /weather <city/state or ZIP>"
        return server.weather_lookup(arg.strip())
    if cmd in ("/work", "/agent"):
        if not arg.strip():
            return "usage: /work <task>"
        task_boundary_error = _account_task_boundary_refusal(
            "workbench_agent", {}, context,
        )
        if task_boundary_error:
            return task_boundary_error
        return server.workbench_agent(
            prompt=arg.strip(), tier="auto", max_steps=12, project=project,
        )
    if cmd in (
        "/report", "/endreport", "/checklist", "/plan",
        "/inventory", "/workspace",
        "/tree", "/folders", "/search", "/grep",
        "/programs", "/programfind", "/scripts", "/scriptfind",
        "/image", "/inspectimage", "/mkdir", "/runprogram", "/runscript",
        "/artifactcheck", "/verifyartifact", "/groundartifact",
    ):
        scope = _task_account_scope(context)
        if scope is not None and cmd in ("/checklist", "/plan"):
            if arg.strip():
                return server.scoped_task_tool_dispatch(
                    "checklist_show", {"checklist_id": arg.strip()}, account_scope=scope,
                )
            return server.scoped_latest_checklist(scope)
        return server.control_command(stripped, project=project)
    if cmd in ("/asset", "/assets", "/assetgen", "/artifact"):
        parts2 = arg.strip().split(None, 1)
        if len(parts2) != 2:
            return "usage: /asset <name> <free-form brief>"
        return server.artifact_generate(name=parts2[0], brief=parts2[1])
    if cmd in ("/forge", "/gamesuite"):
        return server.game_reference_suite(name=arg.strip() or "sonder-reference")
    if cmd in ("/game", "/gamegen"):
        parts2 = arg.strip().split(None, 2)
        if len(parts2) != 3 or "|" not in parts2[2]:
            return "usage: /game <language> <2d|2.5d|3d> <name> | <concept>"
        game_name, _, concept = parts2[2].partition("|")
        return server.game_generate_and_test(
            name=game_name.strip(), concept=concept.strip(),
            language=parts2[0], dimension=parts2[1],
        )
    if cmd in ("/gamefleet", "/gamecampaign"):
        campaign_args = server._parse_game_campaign_command(arg)
        if campaign_args is None:
            return "usage: /gamefleet <name> | <concept> [| language | dimension]"
        return server.game_generation_campaign(**campaign_args)
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
        task_boundary_error = _account_task_boundary_refusal(
            "master_orchestrate", {}, context,
        )
        if task_boundary_error:
            return task_boundary_error
        text = arg.strip()
        mode = "ask"
        task = text
        if text:
            parts = text.split(None, 1)
            mode_alias = {
                "delagte": "delegate", "delegte": "delegate",
                "paralell": "parallel", "inlne": "inline",
                "workflow": "fleet",
            }
            requested_mode = mode_alias.get(parts[0].lower(), parts[0].lower())
            if requested_mode in (
                "ask", "inline", "master", "delegate", "delegated", "agents",
                "parallel", "fleet", "swarm", "fanout",
            ):
                mode = requested_mode
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

    dispatched = _dispatch_catalogued_tool(stripped, state, context=context)
    if dispatched is not None:
        return dispatched

    return None  # not a recognized slash command — fall through to the model


def _dispatch_catalogued_tool(line, state, context=None):
    """Run ``/<tool> ...`` for any registered tool without a branch of its own.

    The explicit branches above cover the curated console commands; everything
    else the server registers -- all 178 tools -- is reachable here by its own
    name, which is the only way the app can offer the whole surface without a
    branch per tool. Returns None when the line names nothing catalogued, so
    an ordinary sentence beginning with "/" still falls through to the model.

    Gated here as well as at the top of the chain, and it has to be: the chain
    gate keys on the *named command*, and this path is reached by the *tool's
    own name*, which no named command covers. Left ungated, `/delete` was
    refused under `plan` while `/file_delete path=x dry_run=false` ran -- the
    same tool, the other spelling, with the tool's own last-resort default
    turned off by the caller. `interactive=False` for the same reason as the
    chain: an HTTP request has nobody to prompt.
    """
    try:
        invocation = command_catalog.parse_invocation(line)
    except ValueError as error:
        # A mistyped key must not run the tool with that argument silently
        # dropped; say so instead of 500ing or half-executing.
        return str(error)
    except command_catalog.CatalogUnavailable as error:
        # Resolving the tool is itself a catalog read. Refuse rather than
        # raise into the request, and rather than dispatch something the gate
        # could not have classified.
        return "refused %s: %s" % (line.split(None, 1)[0], error)
    if not invocation:
        return None
    tool_name, kwargs = invocation
    handler = getattr(server, tool_name, None)
    if not callable(handler):
        return "%s is catalogued but not callable here." % tool_name
    refusal = _http_tool_refusal((tool_name,), "/" + tool_name, context=context)
    if refusal:
        return refusal
    if tool_name == "loop":
        refusal = _loop_global_operation_refusal(kwargs.get("actions_json"), context)
        if refusal:
            return refusal
    task_boundary_error = _account_task_boundary_refusal(tool_name, kwargs, context)
    if task_boundary_error:
        return task_boundary_error
    # Guarded tools take the caller's own token exactly as the explicit
    # branches pass it (/read, /files, /delete); the tool still enforces its
    # own permission rules with it.
    if "token" not in kwargs and getattr(state, "token", ""):
        # Look the command up by the name that was typed: a native alias
        # ("/read") carries the tool ("file_read") but is not catalogued under
        # the tool's own name, so by_name(tool_name) would miss its schema.
        command = command_catalog.by_name(line.split(None, 1)[0])
        if command and any(p.name == "token" for p in command.params):
            kwargs["token"] = state.token
    try:
        if tool_name in _SCOPED_TASK_TOOLS and _task_account_scope(context) is not None:
            return _served_task_tool(tool_name, kwargs, context)
        return str(handler(**kwargs))
    except TypeError as error:
        return "%s: %s" % (tool_name, error)
    except Exception as error:  # a tool fault is a chat answer, not a 500
        return "%s failed: %s: %s" % (tool_name, type(error).__name__, error)


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


def _handle_work_intent(content, project="", authorized=False):
    """Route developer work through the bounded execution-mode chooser."""
    if not authorized:
        return None
    return server.route_work_request(content, project=project)


def _model_to_tier(model):
    """Map an OpenAI `model` field to a Sonder Runtime route.

    "sonder" (and the OpenAI-ish default "gpt-*"/blank) -> the local learning
    route, which resolves to the configured Ollama model or alias.
    Any known tier name (e.g. "cloud-code") selects that model directly."""
    m = (model or "").strip()
    if not m or m == "sonder" or m.startswith("gpt-"):
        return None  # default: local learning route
    if m in server.TIERS:
        return m
    return None


def _request_model_selector(model):
    """Keep OpenAI defaults, but pass explicit non-default selectors to server.

    ``server._serve_target`` performs the live-catalog allowlist check.  Keeping
    it there means MCP, desktop, and HTTP cannot disagree about which concrete
    model names are safe to request.
    """
    selected = _model_to_tier(model)
    if selected is not None:
        return selected
    raw = str(model or "").strip()
    # OpenAI-compatible clients often use gpt-* model identifiers.  Preserve
    # their default fallback *unless* that exact identifier is actually in the
    # live Ollama catalog (for example gpt-oss:20b).
    if raw:
        try:
            discovered = server.resolve_discovered_model(raw)
        except Exception:
            discovered = None
        if discovered:
            return discovered
    if not raw or raw in ("sonder", "local") or raw.startswith("gpt-"):
        return None
    return raw


def _chat_model_selection_error(selector):
    """Return a safe HTTP error for an explicit selector that cannot chat.

    ``server._serve_target`` is the shared allowlist/capability authority for
    every surface.  The HTTP endpoint must translate its ``None`` result into
    a protocol error rather than returning a successful completion whose text
    happens to start with ``ERROR:``.
    """
    if not selector:
        return None
    try:
        _target, _cloud, _augment, tier_label = server._serve_target(selector, None)
        if tier_label == "cloud-disabled":
            return 403, "cloud model access is disabled by the operator"
        if tier_label is not None:
            return None
        found = server.resolve_discovered_model_record(selector)
    except Exception:
        # An explicit exact selector needs the operator's live catalog.  A
        # temporary catalog failure is neither a valid model nor a caller
        # mistake; make it retryable instead of manufacturing a 200 response
        # containing an error string.
        return 503, "live model catalog is temporarily unavailable; retry shortly"
    if found:
        _name, record = found
        reason = server._fanout_nonchat_reason(record)
        if reason:
            return 400, "model is not chat-capable (%s)" % reason
    return 400, "unknown model '%s'" % selector


def _openai_model_rows():
    """Return stable, safe OpenAI-shaped model metadata for chat clients."""
    rows, seen = [], set()

    def add(identifier, owned_by):
        identifier = str(identifier or "").strip()
        key = identifier.casefold()
        if not identifier or key in seen:
            return
        seen.add(key)
        rows.append({"id": identifier, "object": "model", "owned_by": owned_by})

    # ``sonder`` is a runtime route ID, not weights.  Tier IDs are retained for
    # compatibility, then exact live catalog names make valid installed models
    # discoverable to standard OpenAI clients.
    add("sonder", "local")
    for tier_name, model in server.available_tiers().items():
        add(tier_name, "cloud" if server._is_cloud_tier(tier_name, model) else "local")
    try:
        records = server.discovered_model_records()
    except Exception:
        records = ()
    for name, record in records:
        if server._fanout_nonchat_reason(record):
            continue
        cloud = server._is_cloud_model_name(name)
        if cloud and not server.cloud_allowed():
            continue
        add(name, "cloud" if cloud else "local")
    return rows


def _reasoning_audience():
    """Who may receive model reasoning over HTTP: 'developer' or 'all'.

    Only consulted when server.reasoning_exposure_enabled() is on. Defaults to
    the narrower of the two, so turning exposure on does not by itself hand
    reasoning to every caller of a shared deployment.
    """
    value = os.environ.get("SONDER_REASONING_AUDIENCE", "").strip().lower()
    return "all" if value == "all" else "developer"


def _reasoning_visible_to(context):
    if not server.reasoning_exposure_enabled():
        return False
    if _reasoning_audience() == "all":
        return True
    return _developer_authorized(context)


def _turn_reasoning():
    """Reasoning recorded by the span this request is running inside."""
    if not server.reasoning_exposure_enabled():
        return ""
    record = server.activity_tracker.current_reasoning()
    if not record:
        return ""
    return record.get("text") or ""


def _run_prompt(
    prompt, history=None, tier=None, context_size="", session="", project="",
    state=None, return_result=False,
):
    """Call Sonder Runtime's learning loop with the UI's prior turns; returns UI text."""
    state = _state_or_legacy(state)
    resolved_target = {}

    def record_target(model, tier_label, _cloud):
        # This callback is invoked by the generation path after it has accepted
        # the route.  Model/tier names are catalog/config values, but still
        # constrain them before presenting a receipt to an HTTP caller.
        resolved_target["model"] = _receipt_text(model)
        resolved_target["tier"] = _receipt_text(tier_label)

    out = server.answer_with_history(
        prompt, history, trace=state.trace, strict=state.strict, tier=tier,
        context_size=context_size, session=session, project=project,
        raise_model_errors=True, target_observer=record_target,
    )
    if out.startswith("ERROR"):
        result = TurnResult(out, None, "")
        return result if return_result else result.content
    iid = server.parse_interaction_id(out)
    run_source = _answer_only(out)
    state.last_iid = iid
    state.last_response = out
    state.last_run_source = run_source
    result = TurnResult(
        _strip_footer(out), iid, run_source, _turn_reasoning(),
        resolved_target.get("model", ""), resolved_target.get("tier", ""),
    )
    return result if return_result else result.content


def _run_structured_prompt(prompt, history, tier, schema, context_size=""):
    """Run the isolated structured path; never add a footer or activity text."""
    resolved_target = {}

    def record_target(model, tier_label, _cloud):
        resolved_target["model"] = _receipt_text(model)
        resolved_target["tier"] = _receipt_text(tier_label)

    content = server.structured_answer_with_history(
        prompt, history, schema, tier=tier, context_size=context_size,
        target_observer=record_target,
    )
    return TurnResult(
        content, None, content, "", resolved_target.get("model", ""),
        resolved_target.get("tier", ""),
    )


_STRUCTURED_SCHEMA_KEYS = frozenset({
    "type", "enum", "const", "required", "properties",
    "additionalProperties", "minProperties", "maxProperties", "items",
    "minItems", "maxItems", "uniqueItems", "minLength", "maxLength",
    "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf",
})
_STRUCTURED_TYPES = frozenset({
    "object", "array", "string", "integer", "number", "boolean", "null",
})
_STRUCTURED_SCHEMA_MAX_DEPTH = 16
# ``json_schema_verifier`` intentionally compares arbitrary JSON values for
# ``uniqueItems`` rather than relying on hashing. That is the correct JSON
# equality semantics, but is quadratic in the array length. HTTP structured
# output therefore needs an explicit finite host bound before asking a model to
# produce such an array.
# Keep HTTP admission and server-side verification on the same host bound. A
# model can still return more elements than its decoder schema requested.
_STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS = server._STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS


def _response_format_error(message):
    return HTTPRequestError(400, message)


def _response_format_schema(value):
    """Return the fully host-verifiable schema promised by HTTP chat.

    ``json_schema_verifier`` deliberately supports a broader, disclosure-based
    dialect for internal tools. HTTP cannot expose a partial guarantee under an
    OpenAI-compatible ``response_format`` label, so it accepts only this small
    recursive subset and requires ``strict: true`` for ``json_schema``.
    """
    if not isinstance(value, dict):
        raise _response_format_error("response_format must be an object")
    kind = value.get("type")
    if kind == "json_object" and set(value) == {"type"}:
        return {"type": "object"}
    if kind != "json_schema" or set(value) != {"type", "json_schema"}:
        raise _response_format_error(
            "response_format must be {'type':'json_object'} or a strict json_schema object",
        )
    envelope = value["json_schema"]
    if not isinstance(envelope, dict) or set(envelope) != {"name", "schema", "strict"}:
        raise _response_format_error(
            "json_schema must contain exactly name, schema, and strict",
        )
    if (not isinstance(envelope["name"], str) or not envelope["name"].strip()
            or len(envelope["name"]) > 64 or envelope["strict"] is not True):
        raise _response_format_error(
            "json_schema requires a non-empty name (at most 64 characters) and strict=true",
        )
    schema = envelope["schema"]
    _validate_structured_schema(schema)
    return schema


def _validate_structured_schema(schema, depth=0):
    """Reject schema features that this endpoint cannot prove post-hoc."""
    if depth > _STRUCTURED_SCHEMA_MAX_DEPTH:
        raise _response_format_error("response_format schema is nested too deeply")
    if not isinstance(schema, dict):
        raise _response_format_error("response_format schema nodes must be objects")
    unknown = set(schema) - _STRUCTURED_SCHEMA_KEYS
    if unknown:
        raise _response_format_error("response_format schema uses unsupported keywords: %s"
            % ", ".join(sorted(unknown)),
        )
    declared = schema.get("type")
    if not isinstance(declared, str) or declared not in _STRUCTURED_TYPES:
        raise _response_format_error("every response_format schema node needs one supported type")
    if "required" in schema and (
        not isinstance(schema["required"], list)
        or any(not isinstance(key, str) for key in schema["required"])
    ):
        raise _response_format_error("schema required must be an array of strings")
    if "properties" in schema:
        properties = schema["properties"]
        if not isinstance(properties, dict) or any(not isinstance(key, str) for key in properties):
            raise _response_format_error("schema properties must be an object")
        for child in properties.values():
            _validate_structured_schema(child, depth + 1)
    if "additionalProperties" in schema:
        extra = schema["additionalProperties"]
        if isinstance(extra, dict):
            _validate_structured_schema(extra, depth + 1)
        elif not isinstance(extra, bool):
            raise _response_format_error("additionalProperties must be boolean or a schema")
    if "items" in schema:
        _validate_structured_schema(schema["items"], depth + 1)
    for keyword in ("minProperties", "maxProperties", "minItems", "maxItems", "minLength", "maxLength"):
        if keyword in schema and (isinstance(schema[keyword], bool) or not isinstance(schema[keyword], int) or schema[keyword] < 0):
            raise _response_format_error("%s must be a non-negative integer" % keyword)
    if "uniqueItems" in schema and not isinstance(schema["uniqueItems"], bool):
        raise _response_format_error("uniqueItems must be a boolean")
    if schema.get("uniqueItems") is True:
        maximum = schema.get("maxItems")
        if isinstance(maximum, bool) or not isinstance(maximum, int):
            raise _response_format_error(
                "uniqueItems=true requires an integer maxItems no greater than %d"
                % _STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS,
            )
        if maximum > _STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS:
            raise _response_format_error(
                "uniqueItems=true requires maxItems no greater than %d"
                % _STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS,
            )
    for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum", "multipleOf"):
        if keyword in schema and (
            isinstance(schema[keyword], bool)
            or not isinstance(schema[keyword], (int, float))
            # Python integers are exact and inherently finite; converting an
            # enormous valid JSON integer to a C double solely to test it can
            # raise OverflowError. Only floats need a finiteness check.
            or (isinstance(schema[keyword], float) and not math.isfinite(schema[keyword]))
        ):
            raise _response_format_error("%s must be a number" % keyword)
    if "multipleOf" in schema and schema["multipleOf"] <= 0:
        raise _response_format_error("multipleOf must be positive")
    if "enum" in schema and not isinstance(schema["enum"], list):
        raise _response_format_error("enum must be an array")


def _receipt_text(value):
    """Bound a provider/configuration label for response metadata.

    The receipt never contains model output, prompt text, tokens, history, or
    provider diagnostics.  Replacing controls also prevents a malicious or
    malformed catalog label from smuggling a response header-looking value into
    terminal/SSE consumers.
    """
    return "".join(
        char if char >= " " and char != "\x7f" else "?"
        for char in str(value or "")[:256]
    )


def _chat_completion_object(
    content, model="sonder", iid=None, reasoning="", elapsed_ms=None,
    receipt=None,
):
    iid = iid or uuid.uuid4().hex[:12]
    obj = {
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
        "sonder_activity": (
            server.activity_tracker.public_snapshot(include_detail=False) or {}
        ).get("latest"),
    }
    # Mirrors sonder_activity: present only when there is something to show, so
    # clients can treat absence as "this deployment does not expose reasoning".
    if reasoning:
        obj["sonder_reasoning"] = reasoning
    if elapsed_ms is not None:
        # Vendor extension: monotonic wall duration for the complete HTTP
        # request, including routing/tool work, not just model generation.
        obj["sonder_elapsed_ms"] = max(0, int(elapsed_ms))
    if receipt:
        obj["sonder_receipt"] = receipt
    return obj


def _chunk(iid, model, delta, finish_reason=None, elapsed_ms=None, receipt=None):
    obj = {
        "id": "chatcmpl-%s" % iid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
    }
    if elapsed_ms is not None:
        obj["sonder_elapsed_ms"] = max(0, int(elapsed_ms))
    if receipt:
        obj["sonder_receipt"] = receipt
    return "data: %s\n\n" % json.dumps(obj)


COMPLETE_DEFAULT_LIMIT = 12
COMPLETE_MAX_LIMIT = 50


def _completion_limit(raw):
    """Clamp ``?limit=`` into 1..50; anything unreadable takes the default.

    A junk limit is a typo in a URL someone is hand-editing, not an attack.
    Failing the request would blank the completion menu mid-keystroke, so the
    parameter is ignored instead.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return COMPLETE_DEFAULT_LIMIT
    return max(1, min(COMPLETE_MAX_LIMIT, value))


def _commands_index_payload():
    # A catalog that cannot read the tool registry now raises rather than
    # quietly returning nothing (command_catalog.CatalogUnavailable). This is
    # a listing endpoint, not an enforcing one, so it degrades -- but it says
    # so in the payload instead of shipping an empty list the client would
    # render as "this build has no commands".
    try:
        commands = [command.to_dict() for command in command_catalog.catalog()]
        error = ""
    except command_catalog.CatalogUnavailable as exc:
        commands, error = [], str(exc)
    payload = {
        "commands": commands,
        # The blurb per category, not the commands in it: the client renders
        # these as section headings beside the counts it derives itself.
        "categories": dict(command_catalog.CATEGORIES),
        "popular": list(command_catalog.POPULAR),
    }
    if error:
        payload["error"] = error
    return payload


def _commands_complete_payload(query, limit=""):
    try:
        matches = [
            command.to_dict()
            for command in command_catalog.complete(
                query, limit=_completion_limit(limit),
            )
        ]
    except command_catalog.CatalogUnavailable as exc:
        return {"matches": [], "error": str(exc)}
    return {"matches": matches}


def _commands_help_payload(topic=""):
    return {"text": command_catalog.help_text(topic)}


class Handler(BaseHTTPRequestHandler):
    server_version = "sonder-serve/1.0"
    # socketserver reads this in setup() and calls connection.settimeout(); a
    # timed-out read raises in handle_one_request, which closes the connection.
    timeout = REQUEST_TIMEOUT_SECONDS

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin in CORS_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Sonder-Account-Token, "
                "X-Sonder-Bootstrap-Secret",
            )
            self.send_header(
                "Access-Control-Expose-Headers",
                "X-Sonder-Elapsed-Ms, X-Sonder-Correlation-Id",
            )

    def log_message(self, fmt, *args):
        sys.stderr.write("[sonder_serve] %s\n" % (fmt % args))

    def do_OPTIONS(self):
        self._request_started = time.monotonic()
        if self._reject_disallowed_origin():
            return
        self.send_response(204)
        self._cors()
        self.send_header("X-Sonder-Elapsed-Ms", "0")
        self.end_headers()

    def _reject_disallowed_origin(self):
        origin = self.headers.get("Origin")
        if origin is None or origin in CORS_ORIGINS:
            return False
        if (
            self.command == "POST"
            and _request_route(self.path) == "/v1/chat/completions"
        ):
            self._record_chat_completion_metric(
                sonder_lifecycle.get(), "cors_rejected",
                getattr(self, "_request_started", time.monotonic()),
            )
        self._send_json_payload(
            {"error": {"message": "origin is not allowed", "type": "cors"}},
            status=403,
        )
        return True

    def _request_auth_context(self):
        return _auth_context(
            self.headers.get("Authorization", ""),
            self.headers.get("X-Sonder-Account-Token", ""),
        )

    def _peer(self):
        return self.client_address[0] if self.client_address else ""

    def _correlation(self):
        if not getattr(self, "_correlation_id", ""):
            self._correlation_id = sonder_lifecycle.new_correlation_id()
        return self._correlation_id

    def _send_auth_error(self, reason="invalid-credentials"):
        sonder_lifecycle.get().record_auth_failure(self._peer(), reason)
        self._send_json_payload({
            "error": {"message": "authentication required", "type": "auth",
                      "code": "UNAUTHENTICATED",
                      "correlation_id": self._correlation()},
        }, status=401)

    def _auth_rate_limited(self):
        """Token-bucket authentication-failure limiter (admission step 3)."""
        if sonder_lifecycle.get().auth_attempt_allowed(self._peer()):
            return False
        if (
            self.command == "POST"
            and _request_route(self.path) == "/v1/chat/completions"
        ):
            self._record_chat_completion_metric(
                sonder_lifecycle.get(), "auth_rate_limited",
                getattr(self, "_request_started", time.monotonic()),
            )
        self._send_json_payload(
            sonder_lifecycle.error_envelope(
                "AUTH_RATE_LIMITED",
                "too many failed authentication attempts; retry later",
                self._correlation(),
                retryable=True,
            ),
            status=429,
            headers={"Retry-After": "2"},
        )
        return True

    def _peer_is_loopback(self):
        return _is_loopback_host(self._peer())

    def _handle_lifecycle_get(self, path):
        """SPEC-2 WP3 endpoints. Returns True when the path was handled.

        /live is unauthenticated and reveals only {status: alive}.  The
        other lifecycle routes require authentication unless the peer is
        loopback (the reverse proxy restricts them to loopback upstream).
        """
        lifecycle = sonder_lifecycle.get()
        if path == "/live":
            self._send_json_payload(lifecycle.live_payload())
            return True
        if path not in ("/ready", "/health", "/version", "/metrics"):
            return False
        if not self._peer_is_loopback():
            if self._auth_rate_limited():
                return True
            if not self._request_auth_context()["authorized"]:
                self._send_auth_error()
                return True
        if path == "/ready":
            status, payload = lifecycle.ready_payload()
            self._send_json_payload(payload, status=status)
            return True
        if path == "/health":
            self._send_json_payload(lifecycle.health_payload())
            return True
        if path == "/version":
            self._send_json_payload(lifecycle.version_payload())
            return True
        body = lifecycle.metrics.render()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        return True

    def _handle_admin_drain(self):
        """POST /v1/admin/drain — administrator-only, idempotent."""
        if self._auth_rate_limited():
            return
        context = self._request_auth_context()
        if not context["authorized"]:
            self._send_auth_error()
            return
        if not _admin_authorized(context):
            self._send_json_payload(
                sonder_lifecycle.error_envelope(
                    "FORBIDDEN",
                    "administrator authorization is required",
                    self._correlation(),
                    retryable=False,
                ),
                status=403,
            )
            return
        lifecycle = sonder_lifecycle.get()

        def start_drain():
            threading.Thread(
                target=lifecycle.drain,
                kwargs={"reason": "admin drain request"},
                daemon=True,
                name="sonder-admin-drain",
            ).start()
            return {
                "draining": True,
                "correlation_id": self._correlation(),
            }

        payload = lifecycle.idempotent(
            self.headers.get("Idempotency-Key", ""), start_drain
        )
        self._send_json_payload(payload, status=202)

    def _send_not_found(self):
        self._send_json_payload(
            {"error": {"message": "not found", "type": "not_found"}},
            status=404,
        )

    def _sonder_health_nonce(self):
        """Return a valid private challenge without revealing why one failed."""
        client_host = self.client_address[0] if self.client_address else ""
        nonce = self.headers.get(sonder_health.NONCE_HEADER, "")
        if (
            not _is_loopback_host(client_host)
            or not sonder_health.token_is_configured(LAUNCHER_HEALTH_TOKEN)
            or RUNTIME_ROLE != sonder_health.MANAGED_ROLE
            or not sonder_health.nonce_is_valid(nonce)
        ):
            return ""
        return nonce

    def _send_json_payload(self, payload, status=200, headers=None, elapsed_ms=None):
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(status)
            self._cors()
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            if getattr(self, "_correlation_id", ""):
                self.send_header("X-Sonder-Correlation-Id", self._correlation_id)
            started = getattr(self, "_request_started", None)
            if elapsed_ms is not None:
                self.send_header("X-Sonder-Elapsed-Ms", str(max(0, int(elapsed_ms))))
            elif started is not None:
                self.send_header(
                    "X-Sonder-Elapsed-Ms",
                    str(max(0, int((time.monotonic() - started) * 1000))),
                )
            for name, value in (headers or {}).items():
                self.send_header(str(name), str(value))
            self.end_headers()
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # A browser or terminal client may leave while a model finishes.
            # This is a delivery failure, not a server traceback.
            return False

    def _record_chat_completion_metric(self, lifecycle, result, started):
        """Record exactly one terminal metric for a chat-completion request."""
        if getattr(self, "_chat_completion_metrics_recorded", False):
            return
        self._chat_completion_metrics_recorded = True
        lifecycle.metrics.requests_total.labels(
            route="/v1/chat/completions", result=result
        ).inc()
        lifecycle.metrics.request_duration_seconds.labels(
            route="/v1/chat/completions"
        ).observe(max(0.0, time.monotonic() - started))

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
        self._request_started = time.monotonic()
        if self._reject_disallowed_origin():
            return
        path = _request_route(self.path)
        if path == "/" and self._peer_is_loopback():
            self._send_local_log_page()
            return
        if path == "/v1/local/server-log":
            if not self._peer_is_loopback():
                self._send_not_found()
            else:
                self._send_json_payload({"log": _local_server_log_tail()})
            return
        if self._handle_lifecycle_get(path):
            return
        if sonder_health.request_path_matches(self.path):
            nonce = self._sonder_health_nonce()
            if not nonce:
                self._send_not_found()
                return
            port = int(
                getattr(self.server, "server_port", self.server.server_address[1])
            )
            self._send_json_payload(
                sonder_health.response_payload(
                    LAUNCHER_HEALTH_TOKEN,
                    nonce,
                    port,
                    role=RUNTIME_ROLE,
                )
            )
            return
        if self._auth_rate_limited():
            return
        _maybe_live_reload()
        if path == "/v1/models":
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            self._send_json_payload({"object": "list", "data": _openai_model_rows()})
            return
        if path == "/v1/admin/updates/status":
            # Durable update state for the System page (SPEC-4 R-M19).
            # Read-only; install/rollback stay on the admin CLI surface.
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            if not _admin_authorized(context):
                self._send_json_payload(
                    sonder_lifecycle.error_envelope(
                        "FORBIDDEN",
                        "administrator authorization is required",
                        self._correlation(),
                        retryable=False,
                    ),
                    status=403,
                )
                return
            try:
                import sonder_update_engine

                payload = sonder_update_engine.UpdateManager().status()
            except Exception as error:
                self.log_error("update status failed: %s", type(error).__name__)
                self._send_json_payload(
                    sonder_lifecycle.error_envelope(
                        "INTERNAL",
                        "update status is unavailable",
                        self._correlation(),
                        retryable=True,
                    ),
                    status=500,
                )
                return
            self._send_json_payload(payload)
            return
        if path == "/v1/sonder/status":
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            account = context["account"]
            agents = server.master_orchestrator.snapshot()
            activity_source = server.activity_tracker.snapshot()
            detail_allowed = _execution_feed_detail_allowed(context)
            activity = server.activity_tracker.public_snapshot(
                activity_source, include_detail=detail_allowed,
            )
            payload = {
                "status": server.status(),
                "stats": server.sonder_stats(),
                "learn_tiers": server.learn_tiers(),
                "improvements": server.system_improvement_report(),
                "context": server.context_health_data(),
                "context_policy": server.context_policy.policy(server.SESSION_NUM_CTX),
                "agents": agents,
                "execution": server.execution_status_data(
                    agents, activity_source, include_detail=detail_allowed,
                ),
                "autopilot": server.autopilot_controller.snapshot(),
                "runtime_policy": server.runtime_policy_data(),
                "selfmod": server.selfmod.status_data(),
                "mcp_runtime": server.mcp_runtime_data(),
                "npu_fallback": server.npu_fallback_status_data(),
                "learning_health": server.learning_health_data(),
                "activity": activity,
                "db_path": getattr(server, "_DB_PATH", ""),
                "state_home": str(server.sonder_paths.default_home()),
                "account": account or {},
                "models": [
                    {"id": "sonder", "owned_by": "local"},
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
            self._send_json_payload(payload)
            return
        if self._handle_commands_get():
            return
        if self._handle_permission_mode_get():
            return
        if self._handle_fanout_get():
            return
        self._send_not_found()

    def _send_local_log_page(self):
        """Serve the loopback-only browser landing page without auth state."""
        body = _LOCAL_LOG_PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _handle_commands_get(self):
        """Command catalog, completion, and help for the app's command bar.

        Read-only over the same gate the neighbouring GET routes use: the
        catalog names every tool but runs none of them.
        """
        split = urllib.parse.urlsplit(self.path)
        route = split.path.rstrip("/") or "/"
        if route not in (
            "/v1/commands", "/v1/commands/complete", "/v1/commands/help",
        ):
            return False
        if not self._request_auth_context()["authorized"]:
            self._send_auth_error()
            return True
        query = urllib.parse.parse_qs(split.query, keep_blank_values=True)

        def first(name):
            values = query.get(name) or [""]
            return values[0]

        if route == "/v1/commands/complete":
            payload = _commands_complete_payload(first("q"), first("limit"))
        elif route == "/v1/commands/help":
            payload = _commands_help_payload(first("topic"))
        else:
            payload = _commands_index_payload()
        self._send_json_payload(payload)
        return True

    def _handle_permission_mode_get(self):
        """Current autonomy mode, so a client can show it before you send.

        Read-only. Setting the mode is a POST; a GET must never change it.
        """
        route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if route != "/v1/permission-mode":
            return False
        if not self._request_auth_context()["authorized"]:
            self._send_auth_error()
            return True
        self._send_json_payload(server.permission_mode_data())
        return True

    def _fanout_run_for_context(self, context, run_id):
        """Return a lifecycle receipt only to its shared-deployment owner.

        Fanout answers are durable and can contain a caller's private prompt
        context.  Developers may launch fanouts, but that alone must not make
        every other developer's receipt readable.  Administrators retain the
        explicit operational recovery path; local-open remains single-user.
        """
        if not _developer_authorized(context):
            return None, (403, "developer or admin authentication is required for model fanout")
        run = server.fanout_store.get_run(run_id)
        if run is None:
            return None, (404, "fanout run was not found")
        account = context.get("account") or {}
        is_admin = account.get("role") == "admin"
        if context.get("mode") != "local-open" and not is_admin:
            if run.get("request_owner") != _fanout_request_owner(context):
                # Do not disclose another developer's run identifier.
                return None, (404, "fanout run was not found")
        return run, None

    def _handle_fanout_get(self):
        route = urllib.parse.urlsplit(self.path).path.rstrip("/")
        if route == "/v1/fanout":
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return True
            if not _developer_authorized(context):
                self._send_json_payload({"error": {"message": "developer or admin authentication is required for model fanout", "type": "forbidden"}}, status=403)
                return True
            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query, keep_blank_values=True,
            )
            # A history query is a small, explicitly bounded contract.  Do
            # not let duplicate values acquire accidental first-value-wins
            # semantics through a proxy or a client encoder.
            for name in ("limit", "include_finished"):
                if len(query.get(name, ())) > 1:
                    self._send_json_payload({"error": {"message": "%s must be supplied at most once" % name, "type": "invalid_request"}}, status=400)
                    return True
            limit_text = (query.get("limit") or ["20"])[0]
            finished_text = (query.get("include_finished") or ["true"])[0].casefold()
            try:
                limit = int(limit_text)
            except (TypeError, ValueError):
                self._send_json_payload({"error": {"message": "limit must be an integer between 1 and 100", "type": "invalid_request"}}, status=400)
                return True
            if not 1 <= limit <= 100:
                self._send_json_payload({"error": {"message": "limit must be an integer between 1 and 100", "type": "invalid_request"}}, status=400)
                return True
            if finished_text not in ("true", "false"):
                self._send_json_payload({"error": {"message": "include_finished must be true or false", "type": "invalid_request"}}, status=400)
                return True
            account = context.get("account") or {}
            request_owner = None
            if context.get("mode") != "local-open" and account.get("role") != "admin":
                request_owner = _fanout_request_owner(context)
            self._send_json_payload({"runs": server.fanout_store.recent_run_summaries(
                request_owner=request_owner, include_finished=finished_text == "true",
                limit=limit,
            )})
            return True
        prefix = "/v1/fanout/"
        if not route.startswith(prefix) or "/" in route[len(prefix):]:
            return False
        run_id = route[len(prefix):]
        if not run_id or len(run_id) > 80:
            self._send_json_payload({"error": {"message": "invalid fanout run id", "type": "invalid_request"}}, status=400)
            return True
        context = self._request_auth_context()
        if not context["authorized"]:
            self._send_auth_error()
            return True
        _run, error = self._fanout_run_for_context(context, run_id)
        if error:
            status, message = error
            self._send_json_payload({"error": {"message": message, "type": "forbidden" if status == 403 else "not_found"}}, status=status)
            return True
        self._send_json_payload(server._fanout_receipt(run_id))
        return True

    def _handle_fanout_post(self, path, req, context):
        """Mutate or locally synthesize one caller-authorized fanout receipt."""
        prefix = "/v1/fanout/"
        if not path.startswith(prefix):
            return False
        suffix = path[len(prefix):].strip("/")
        parts = suffix.split("/")
        if (len(parts) != 2 or parts[1] not in ("cancel", "resume", "synthesize")
                or not parts[0] or len(parts[0]) > 80):
            return False
        run_id, action = parts
        if not context["authorized"]:
            self._send_auth_error()
            return True
        _run, error = self._fanout_run_for_context(context, run_id)
        if error:
            status, message = error
            self._send_json_payload({"error": {"message": message, "type": "forbidden" if status == 403 else "not_found"}}, status=status)
            return True
        if action == "synthesize":
            if set(req) - {"synth_model"}:
                self._send_json_payload({"error": {"message": "synthesis accepts only synth_model", "type": "invalid_request"}}, status=400)
                return True
            synth_model = req.get("synth_model", "")
            if not isinstance(synth_model, str):
                self._send_json_payload({"error": {"message": "synth_model must be a string", "type": "invalid_request"}}, status=400)
                return True
            # Synthesis starts a fresh bounded local generation.  In shared
            # deployments it must consume the same per-account admission
            # budget as chat completions, otherwise callers can bypass the
            # inference rate limit by repeatedly synthesizing one receipt.
            conn = server._open_db()
            try:
                ok, message = admin_auth.rate_limit(conn, context.get("account"))
            finally:
                conn.close()
            if not ok:
                self._send_json_payload(
                    {"error": {"message": message, "type": "rate_limit"}},
                    status=429,
                )
                return True
            try:
                self._send_json_payload(server._fanout_synthesize_run(_run, synth_model))
            except server.ModelCallError as exc:
                status = exc.status or (
                    400 if exc.kind == "configuration" else
                    504 if exc.kind == "timeout" else 502
                )
                if status == 408:
                    status = 504
                if status not in (400, 403, 404, 429, 502, 503, 504):
                    status = 502
                error_type = "invalid_request_error" if 400 <= status < 500 else "server_error"
                headers = None
                if status in (429, 503, 504):
                    wait = exc.retry_after_seconds
                    retry_after = 1 if wait is None else max(0, int(round(wait)))
                    headers = {"Retry-After": str(retry_after)}
                self._send_json_payload(
                    {"error": {"message": exc.detail, "type": error_type}},
                    status=status, headers=headers,
                )
            return True
        if action == "cancel":
            server.fanout_store.request_cancel(run_id)
        else:
            for name in ("include_failed", "retry_unknown"):
                if name in req and not isinstance(req[name], bool):
                    self._send_json_payload({"error": {"message": "%s must be a boolean" % name, "type": "invalid_request"}}, status=400)
                    return True
            resumed = server.fanout_store.resume_run(
                run_id, include_failed=req.get("include_failed") is True,
                retry_unknown=req.get("retry_unknown") is True,
            )
            if resumed is None:
                self._send_json_payload({"error": {"message": "fanout run is not resumable with the selected retry options", "type": "invalid_request"}}, status=400)
                return True
            # A resume is an explicit replay instruction. _execute preserves
            # the stored snapshot and never retries unknown rows unless this
            # request included retry_unknown=true.
            server._execute_fanout_run(run_id)
        receipt = server._fanout_receipt(run_id)
        self._send_json_payload(receipt or {"error": {"message": "fanout receipt was unavailable", "type": "not_found"}}, status=200 if receipt else 404)
        return True

    def _handle_permission_mode_post(self, req):
        """Switch the autonomy mode. Deliberately cannot grant elevation."""
        wanted = ""
        if isinstance(req, dict):
            wanted = str(req.get("mode") or "").strip()
        if not wanted:
            self._send_json_payload(
                {"error": "mode is required", "modes": list(permission_modes.MODES)},
                status=400,
            )
            return
        try:
            permission_modes.set_mode(wanted)
        except ValueError as exc:
            self._send_json_payload(
                {"error": str(exc), "modes": list(permission_modes.MODES)},
                status=400,
            )
            return
        self._send_json_payload(server.permission_mode_data())

    def do_POST(self):
        self._request_started = time.monotonic()
        # BaseHTTPRequestHandler reuses this instance for HTTP/1.1 keep-alive
        # requests. The terminal-metric latch is per request, never per socket.
        self._chat_completion_metrics_recorded = False
        is_chat_completion = _request_route(self.path) == "/v1/chat/completions"

        def record_early_chat_metric(result):
            if is_chat_completion:
                self._record_chat_completion_metric(
                    sonder_lifecycle.get(), result, self._request_started,
                )

        if self._reject_disallowed_origin():
            return
        _maybe_live_reload()
        path = _request_route(self.path)
        self._correlation()
        if path == "/v1/admin/drain":
            self._handle_admin_drain()
            return
        if self._auth_rate_limited():
            return
        try:
            req = self._read_json()
        except HTTPRequestError as error:
            record_early_chat_metric("malformed_request")
            self._send_json_payload(
                {"error": {"message": error.message, "type": error.error_type}},
                status=error.status,
            )
            return
        context = self._request_auth_context()
        if self._handle_fanout_post(path, req, context):
            return
        if path == "/v1/permission-mode":
            if not context["authorized"]:
                self._send_auth_error()
                return
            # This changes process-global autonomy for every caller.  A valid
            # ordinary account is authentication, not authority to alter a
            # different user's execution policy.
            authority_error = _system_operation_authority_error(
                "permission_mode_change", context,
            )
            if authority_error:
                self._send_json_payload(
                    sonder_lifecycle.error_envelope(
                        "FORBIDDEN",
                        authority_error + " to change permission mode",
                        self._correlation(),
                        retryable=False,
                    ),
                    status=403,
                )
                return
            self._handle_permission_mode_post(req)
            return
        if path == "/v1/sonder/register":
            conn = server._open_db()
            try:
                account = admin_auth.register(
                    conn,
                    req.get("username", ""),
                    req.get("password", ""),
                    trusted_local=False,
                    bootstrap_secret=self.headers.get(
                        "X-Sonder-Bootstrap-Secret", ""
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
        if path == "/v1/sonder/login":
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
        if path == "/v1/sonder/admin/account":
            if not context["authorized"]:
                self._send_auth_error()
                return
            account = context["account"]
            ok, msg = admin_auth.require(account, "admin")
            if not ok:
                self._send_json_payload({"ok": False, "message": msg}, status=403)
                return
            account_header = self.headers.get("X-Sonder-Account-Token", "")
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
            record_early_chat_metric("unauthenticated")
            self._send_auth_error()
            return
        account = context["account"]
        try:
            messages = _validate_chat_messages(req.get("messages"))
        except HTTPRequestError as error:
            record_early_chat_metric("invalid_messages")
            self._send_json_payload(
                {"error": {"message": error.message, "type": error.error_type}},
                status=error.status,
            )
            return
        structured_schema = None
        if "response_format" in req:
            try:
                structured_schema = _response_format_schema(req["response_format"])
            except HTTPRequestError as error:
                record_early_chat_metric("invalid_response_format")
                self._send_json_payload(
                    {"error": {"message": error.message, "type": error.error_type}},
                    status=error.status,
                )
                return
        prompt = _last_user_message(messages)
        # Normalize an explicitly recognized whole-turn model request before
        # policy checks.  Otherwise ``use model x: /run ...`` could evade the
        # initial slash-command gate and execute after the rewrite below.
        natural_model = server.natural_model_request(prompt)
        if structured_schema is not None and natural_model is not None:
            record_early_chat_metric("structured_control_route")
            self._send_json_payload(
                {"error": {"message": "response_format is unavailable for natural model/control routes", "type": "invalid_request"}},
                status=400,
            )
            return
        if natural_model:
            selected_prompt = natural_model["prompt"]
            if selected_prompt.lstrip().startswith("/"):
                record_early_chat_metric("wrapped_slash_command")
                self._send_json_payload(
                    {"error": {"message": "model selection cannot wrap a slash command; issue the command directly.", "type": "invalid_request"}},
                    status=400,
                )
                return
            # A single selected model rewrites the ordinary model prompt. A
            # fanout wrapper must remain intact until its dedicated dispatch
            # below; otherwise its extracted text could be misclassified by
            # feedback/work-intent handlers and execute before fanout gating.
            if natural_model["kind"] == "model":
                prompt = selected_prompt
        if structured_schema is not None and prompt.lstrip().startswith("/"):
            record_early_chat_metric("structured_control_route")
            self._send_json_payload(
                {"error": {"message": "response_format is unavailable for slash/tool/control routes", "type": "invalid_request"}},
                status=400,
            )
            return
        if _dangerous_http_slash(prompt) and not _developer_authorized(context):
            record_early_chat_metric("forbidden_command")
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
            record_early_chat_metric("rate_limited")
            self._send_json_payload({"error": {"message": msg, "type": "rate_limit"}}, status=429)
            return

        stream = req.get("stream", False)
        if stream is None:
            stream = False
        elif not isinstance(stream, bool):
            record_early_chat_metric("invalid_stream")
            self._send_json_payload(
                {"error": {
                    "message": "stream must be a boolean",
                    "type": "invalid_request",
                }},
                status=400,
            )
            return
        model = req.get("model", "sonder")
        if not isinstance(model, str):
            record_early_chat_metric("invalid_model")
            self._send_json_payload(
                {"error": {
                    "message": "model must be a string",
                    "type": "invalid_request",
                }},
                status=400,
            )
            return
        if natural_model and natural_model["kind"] == "fanout":
            # A whole-catalog request spends several model calls.  Local-open
            # keeps its single-user/full-tool behavior; shared deployments
            # require the same developer authority as /ensemble.
            if not _developer_authorized(context):
                record_early_chat_metric("fanout_forbidden")
                self._send_json_payload(
                    {"error": {"message": "developer or admin authentication is required for model fanout", "type": "forbidden_command"}},
                    status=403,
                )
                return
        if natural_model and natural_model["kind"] == "model":
            model = natural_model["model"]
        model_selector = _request_model_selector(model)
        model_error = _chat_model_selection_error(model_selector)
        if model_error:
            status, message = model_error
            record_early_chat_metric("invalid_model")
            self._send_json_payload(
                {"error": {
                    "message": message,
                    "type": "forbidden" if status == 403 else "invalid_request",
                }},
                status=status,
            )
            return
        # Speculatively load the target model now, overlapping its cold-load
        # cost with the history assembly, scope resolution, and memory work
        # below. Best-effort and local-only (see server.prewarm_model).
        try:
            server.prewarm_model(model_selector or "")
        except Exception:
            pass
        context_size = req.get("context_size", "")
        location_consent = req.get("location_consent") is True
        location_hint = req.get("location_hint")
        if location_hint is not None and not isinstance(location_hint, dict):
            record_early_chat_metric("invalid_location_hint")
            self._send_json_payload(
                {
                    "error": {
                        "message": "location_hint must be an object",
                        "type": "invalid_request",
                    }
                },
                status=400,
            )
            return
        try:
            session = _http_scope_value(req.get("session", ""), "session")
            project = _http_scope_value(req.get("project", ""), "project")
            storage_session = _hosted_storage_id(context, session, "session")
            storage_project = _hosted_storage_id(context, project, "project")
        except HTTPRequestError as error:
            record_early_chat_metric("invalid_scope")
            self._send_json_payload(
                {"error": {"message": error.message, "type": error.error_type}},
                status=error.status,
            )
            return
        history = _history_from_messages(messages)
        if not history and storage_session:
            # Thin client: named a session, resent no transcript.
            history = _server_side_history(storage_session)
        account_header = self.headers.get("X-Sonder-Account-Token", "")
        auth_header = self.headers.get("Authorization", "")
        state = _http_conversation_state(
            context,
            session,
            token=_request_account_token(context, auth_header, account_header),
        )

        reply = None
        web_routed = False
        execution_routed = False
        turn = None
        response_iid = None
        response_reasoning = ""
        response_model = ""
        response_tier = ""
        activity_response = None
        _lifecycle = sonder_lifecycle.get()
        _request_started = time.monotonic()
        try:
            # SPEC-2 WP4 admission: bounded concurrency slot with queue
            # depth, admission deadline, drain and maintenance awareness.
            with _lifecycle.acquire_request_slot(mutating=True), state.lock:
                _record_chat("user", prompt, state=state)
                with server.activity_tracker.response_span(
                    "chat:%s" % (model or "sonder"),
                    prompt,
                    surface="http",
                    model=model,
                    session=storage_session,
                    project=storage_project,
                ) as activity_response:
                    if structured_schema is not None:
                        turn = _run_structured_prompt(
                            prompt, history, model_selector, structured_schema,
                            context_size=context_size,
                        )
                        content = turn.content
                        response_model = turn.resolved_model
                        response_tier = turn.resolved_tier
                    else:
                        reply = _handle_slash(
                            prompt, messages=messages, state=state,
                            project=storage_project, context=context,
                        )
                    if (structured_schema is None and reply is None
                            and not (natural_model and natural_model["kind"] == "fanout")):
                        reply = _handle_feedback(prompt, state=state)
                    if (structured_schema is None and reply is None
                            and _developer_authorized(context)
                            and not (natural_model and natural_model["kind"] == "fanout")):
                        reply = _handle_intent(
                            prompt, messages=messages, state=state
                        )
                    if structured_schema is None and reply is None and natural_model and natural_model["kind"] == "fanout":
                        # Developer authority was established above from this
                        # request's HTTP auth context.  Do not call the public
                        # MCP wrapper: it intentionally has no knowledge of
                        # HTTP principals and would reject an API-key owner or
                        # developer account a second time.
                        reply = server._model_fanout_authorized(
                            natural_model["prompt"], scope=natural_model["scope"],
                            profile=natural_model.get("profile", ""),
                            request_owner=_fanout_request_owner(context),
                            request_role=_fanout_request_role(context),
                        )
                    if structured_schema is None and reply is None:
                        reply = server.chat_web_response(
                            prompt,
                            history=history,
                            tier=model_selector or "code",
                            location_consent=location_consent,
                            location_hint=location_hint,
                            allow_server_location_lookup=(
                                location_consent
                                and bool(self.client_address)
                                and _is_loopback_host(self.client_address[0])
                            ),
                        )
                        web_routed = reply is not None
                    if structured_schema is None and reply is None:
                        reply = _handle_work_intent(
                            prompt,
                            project=storage_project,
                            authorized=_developer_authorized(context),
                        )
                        execution_routed = reply is not None
                    if structured_schema is None and reply is not None:
                        content = reply
                    elif structured_schema is None:
                        turn = _run_prompt(
                            prompt,
                            history,
                            model_selector,
                            context_size=context_size,
                            session=storage_session,
                            project=storage_project,
                            state=state,
                            return_result=True,
                        )
                        content = turn.content
                        response_iid = turn.iid
                        response_reasoning = turn.thinking
                        response_model = turn.resolved_model
                        response_tier = turn.resolved_tier
                if structured_schema is None:
                    content = server._append_activity(
                        content, response=activity_response, replace=True,
                    )
                _record_chat(
                    "assistant",
                    content,
                    kind=(
                        "web" if web_routed else
                        "execution" if execution_routed else
                        "slash" if reply is not None else "model"
                    ),
                    state=state,
                )
        except sonder_lifecycle.AdmissionRejected as rejection:
            self._record_chat_completion_metric(
                _lifecycle, rejection.code.lower(),
                getattr(self, "_request_started", _request_started),
            )
            self._send_json_payload(
                sonder_lifecycle.error_envelope(
                    rejection.code,
                    str(rejection),
                    self._correlation(),
                    retryable=rejection.retryable,
                ),
                status=rejection.status,
                headers={"Retry-After": "1"} if rejection.retryable else None,
            )
            return
        except server.ModelCallError as error:
            self._record_chat_completion_metric(
                _lifecycle, "model_error",
                getattr(self, "_request_started", _request_started),
            )
            status = error.status or (
                502 if error.kind in ("protocol", "empty_response", "request")
                else 504 if error.kind == "timeout"
                else 503
            )
            if status == 408:
                status = 504
            if status not in (400, 401, 403, 404, 408, 413, 429, 500, 502, 503, 504):
                status = 502
            error_type = (
                "rate_limit_error" if status == 429 else
                "invalid_request_error" if 400 <= status < 500 else
                "server_error"
            )
            retry_after = "1"
            if error.retry_after_seconds is not None:
                retry_after = str(max(0, int(round(error.retry_after_seconds))))
            headers = (
                {"Retry-After": retry_after}
                if status in (429, 503, 504) else None
            )
            message = error.detail
            if error.cloud and error.status == 429:
                message = server._format_model_call_error(error)
            self._send_json_payload(
                {
                    "error": {
                        "message": message,
                        "type": error_type,
                    }
                },
                status=status,
                headers=headers,
            )
            return
        except Exception as error:
            self.log_error("request failed: %s", type(error).__name__)
            self._record_chat_completion_metric(
                _lifecycle, "error",
                getattr(self, "_request_started", _request_started),
            )
            self._send_json_payload(
                {"error": {"message": "internal server error",
                           "type": "server_error",
                           "correlation_id": self._correlation()}},
                status=500,
            )
            return

        # The handler timer starts before body parsing, authentication, and
        # routing.  It is the public HTTP contract, unlike the inner request
        # timer which exists only for the lifecycle histogram.
        request_started = getattr(self, "_request_started", _request_started)
        if not _reasoning_visible_to(context):
            response_reasoning = ""
        elapsed_ms = int((time.monotonic() - request_started) * 1000)
        receipt = {
            "request_id": self._correlation(),
            "elapsed_ms": max(0, elapsed_ms),
        }
        # Slash, web, and locally synthesized responses do not have a model
        # target.  Omit it instead of reflecting the request selector as fact.
        if response_model and response_tier:
            receipt["model"] = response_model
            receipt["tier"] = response_tier
        if stream:
            streamed = self._send_stream(
                content, model, iid=response_iid, elapsed_ms=elapsed_ms,
                receipt=receipt,
            )
            self._record_chat_completion_metric(
                _lifecycle, "ok" if streamed else "cancelled", request_started,
            )
        else:
            try:
                self._send_json(
                    _chat_completion_object(
                        content, model, iid=response_iid,
                        reasoning=response_reasoning, elapsed_ms=elapsed_ms,
                        receipt=receipt,
                    ), elapsed_ms=elapsed_ms,
                )
            finally:
                # A client can close after generation but before the JSON body
                # is written. That is still a terminal server-side request and
                # must not silently disappear from latency observability.
                self._record_chat_completion_metric(_lifecycle, "ok", request_started)

    def _send_error_completion(self, text, stream):
        if stream:
            self._send_stream(text, "sonder")
        else:
            self._send_json(_chat_completion_object(text, "sonder"))

    def _send_json(self, obj, elapsed_ms=None):
        self._send_json_payload(obj, elapsed_ms=elapsed_ms)

    def _send_stream(self, content, model, iid=None, elapsed_ms=None, receipt=None):
        iid = iid or uuid.uuid4().hex[:12]
        try:
            self.send_response(200)
            self._cors()
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            if elapsed_ms is None:
                started = getattr(self, "_request_started", None)
                elapsed_ms = int((time.monotonic() - started) * 1000) if started else 0
            self.send_header("X-Sonder-Elapsed-Ms", str(max(0, int(elapsed_ms))))
            if getattr(self, "_correlation_id", ""):
                self.send_header("X-Sonder-Correlation-Id", self._correlation_id)
            # No Content-Length on an SSE body — signal end-of-response by closing the
            # connection, otherwise HTTP/1.1 keep-alive leaves clients blocked on read().
            self.send_header("Connection", "close")
            self.close_connection = True
            self.end_headers()
            self.wfile.write(_chunk(iid, model, {"role": "assistant", "content": content}).encode("utf-8"))
            self.wfile.write(_chunk(
                iid, model, {}, finish_reason="stop", elapsed_ms=elapsed_ms,
                receipt=receipt,
            ).encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Header writes can fail before the first event just as body writes
            # can.  Return the same cancellation signal so the caller records
            # one truthful terminal metric instead of leaking a socket error.
            self.close_connection = True
            return False


def main():
    port = DEFAULT_PORT
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            pass
    else:
        port = int(os.environ.get("SONDER_PORT", DEFAULT_PORT))

    _validate_bind_security(HOST)
    lifecycle = sonder_lifecycle.get()
    try:
        # STARTING -> MIGRATING -> READY; no listener opens on failure.
        lifecycle.startup()
    except Exception as error:
        print("startup failed before bind: %s" % error, file=sys.stderr)
        raise SystemExit(1)
    lifecycle.begin_ollama_probe()
    httpd = ThreadingHTTPServer((HOST, port), Handler)
    # After a drain completes (signal or /v1/admin/drain), stop accepting.
    lifecycle.coordinator.add_flush_hook(
        lambda: threading.Thread(
            target=httpd.shutdown, daemon=True, name="sonder-httpd-shutdown"
        ).start()
    )
    global BOUND_PORT
    BOUND_PORT = port
    url = "http://%s:%d" % (HOST, port)
    print("sonder_serve listening on %s" % url)
    print("auth mode: %s" % _effective_auth_mode())
    try:
        # Do not let a Git network timeout delay the first request.  This
        # reports cached origin/main state; `/updatecheck` is the explicit
        # refresh operation.
        print(server.runtime_source_update_status(refresh=False))
    except Exception as exc:
        print("runtime source update status unavailable: %s" % type(exc).__name__)
    print("point your chat UI's OpenAI API base at %s/v1" % url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if not lifecycle.coordinator.draining:
            lifecycle.drain("server stopping")
        httpd.server_close()


if __name__ == "__main__":
    main()
