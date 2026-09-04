"""sonder_serve — OpenAI-compatible HTTP proxy in front of Sonder Runtime's learning loop.

Lets any OpenAI-compatible chat UI (Open WebUI, etc.) talk to server.sonder()
instead of raw Ollama, including the REPL's slash-command powers (/stats, /pass,
/fail, /trace, /strict). Stdlib only (http.server / json / urllib) — zero-dep,
matching the rest of this project.

Run:
    python -m sonder_runtime serve [port]
    (or set env SONDER_PORT; default 11435)

Point your chat UI's OpenAI API base at http://127.0.0.1:<port>/v1 (any api key).
"""
import json
import contextlib
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

import logging as _logging_module
_serve_logger = _logging_module.getLogger(__name__)

from sonder_runtime.adapters.security.permission_policy import permission_policy

# Compatibility name for callers/tests that patch the old HTTP module seam;
# the provider object is shared with all canonical permission calls above.
permission_modes = permission_policy
import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
import sonder_runtime.adapters.observability.chat_formatting as chat_formatting
from sonder_runtime.adapters.command_completion import (
    COMPLETE_DEFAULT_LIMIT,
    COMPLETE_MAX_LIMIT,
    completion_limit as _completion_limit,
)
from sonder_runtime.adapters.command_catalog import command_catalog
from sonder_runtime.adapters.security.account_auth import account_auth as admin_auth
from sonder_runtime.adapters.execution_tools import code_runner, grounding
from sonder_runtime.adapters.content_services import feedback, intents, training_tasks
from sonder_runtime.adapters.web import live_reload
from sonder_runtime.platform import debug_dump
from sonder_runtime.domain import launcher_health as sonder_health
from sonder_runtime.domain.launcher_health import token_is_configured
import sonder_runtime.adapters.web.lifecycle as sonder_lifecycle
import sonder_runtime.platform.config as runtime_config
import sonder_runtime.platform.paths as runtime_paths
import sonder_runtime.adapters.secrets as sonder_secrets
from sonder_runtime.adapters.persistence import served_action_receipts
from . import authority_contract as tool_contract
from sonder_runtime.adapters.security import unsafe_lab
from sonder_runtime.interfaces.http.facades import HealthStatusFacade
from sonder_runtime.interfaces.http.facades.control_plane import ControlPlaneFacade
from sonder_runtime.interfaces.http.facades.a2a import A2AAgentCardFacade
from sonder_runtime.interfaces.http.facades.a2a_jsonrpc import (
    build_application_a2a_handler,
    dispatch_a2a_jsonrpc_route,
)
from sonder_runtime.interfaces.http.facades.extensions import dispatch_extension_route
from sonder_runtime.interfaces.http.facades.observability import dispatch_trace_route
from sonder_runtime.interfaces.http.facades.session import dispatch_session_route
from sonder_runtime.interfaces.http.facades.model_request import (
    ModelFacadeError,
    ModelRequestFacade,
)
from sonder_runtime.domain.common.errors import Conflict, DependencyUnavailable, InvalidInput, NotFound
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.application.execution.world_control import OutputWatermark
from sonder_runtime.application.ports.model_gateway import ModelRequest
from sonder_runtime.application.extensions.facade import ExtensionAuthority


_LEGACY_RUNTIME = None


def configure_legacy_runtime(runtime):
    """Inject the legacy runtime used by the compatibility HTTP routes.

    The HTTP adapter deliberately does not import, discover, or construct the
    historical runtime.  Its route handlers retain their established
    ``server.*`` calls through the explicit injection boundary below.  A
    missing runtime is an operational dependency failure, never an implicit
    fallback or partially initialized request path.
    """
    if runtime is None:
        raise DependencyUnavailable(
            "HTTP legacy runtime must be supplied to configure_legacy_runtime"
        )
    global _LEGACY_RUNTIME
    _LEGACY_RUNTIME = runtime
    _serve_logger.info("Legacy runtime injected into HTTP adapter")
    _serve_logger.debug("configure_legacy_runtime: runtime injected")
    return runtime


def configure_session_facade(facade):
    """Inject the typed durable-session facade at composition time."""
    global _SESSION_FACADE
    _SESSION_FACADE = facade
    _serve_logger.info("Session facade configured for HTTP adapter")
    _serve_logger.debug("configure_session_facade: session facade injected")
    return facade


def configure_a2a_request_handler(handler):
    """Inject the explicitly authorized application-owned A2A handler."""
    if handler is not None and not callable(handler):
        raise TypeError("A2A request handler must be callable or None")
    global _A2A_REQUEST_HANDLER
    _A2A_REQUEST_HANDLER = handler
    _serve_logger.info(f"A2A request handler configured, handler_set={handler is not None}")
    return handler


def configure_control_plane_service(service):
    """Inject the composed read-only operator snapshot service."""
    global _CONTROL_PLANE_SERVICE
    _CONTROL_PLANE_SERVICE = service
    _serve_logger.info("Control plane service configured for HTTP adapter")
    return service


_THIN_HANDLERS: dict = {}


def configure_thin_handlers(handlers: dict) -> None:
    """Register SPEC-5 thin HTTP handlers by path."""
    global _THIN_HANDLERS
    _THIN_HANDLERS = dict(handlers)
    _serve_logger.info(f"SPEC-5 thin handlers configured: {sorted(_THIN_HANDLERS)}")


def _legacy_runtime():
    runtime = _LEGACY_RUNTIME
    if runtime is None:
        raise DependencyUnavailable(
            "HTTP legacy runtime is unavailable; call configure_legacy_runtime"
        )
    return runtime


class _InjectedLegacyRuntime:
    """Compatibility namespace delegating every access to the injected port."""

    def __getattr__(self, name):
        # Exception types are part of the adapter's import-time compatibility
        # surface; they must remain available before bootstrap injection.
        if name == "ModelCallError":
            return ModelCallError
        return getattr(_legacy_runtime(), name)

    def __setattr__(self, name, value):
        """Forward test/runtime patch points without owning legacy state."""
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        setattr(_legacy_runtime(), name, value)


# Keep the established route implementation readable while making every
# legacy access explicit, injectable, and fail-closed.
server = _InjectedLegacyRuntime()

DEFAULT_PORT = 11435
CONFIGURED_PORT = DEFAULT_PORT
_LOCAL_LOG_TAIL_BYTES = 64 * 1024
_TRUSTED_PROXY_NETWORKS: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = (
    ipaddress.ip_network("127.0.0.1/32"),
    ipaddress.ip_network("::1/128"),
)
_HEALTH_STATUS_FACADE = HealthStatusFacade()
_CONTROL_PLANE_FACADE = ControlPlaneFacade()
_A2A_AGENT_CARD_FACADE = A2AAgentCardFacade()
_A2A_REQUEST_HANDLER = None
_MODEL_REQUEST_FACADE = ModelRequestFacade()
_SESSION_FACADE = None
_CONTROL_PLANE_SERVICE = None


class _LiveSessionCaptureFailure(RuntimeError):
    """The HTTP turn cannot claim durable recovery evidence."""


def _capture_live_session_turn(*, session_id, prompt, history, model, content,
                               request_id, turn_id, stream):
    """Capture only a named, model-backed HTTP turn through the app graph.

    Legacy control/web/execution routes intentionally do not enter this seam.
    A capture error is opaque at the HTTP boundary and fails the request closed
    so a successful response is never presented as durably recoverable.
    """
    if not session_id:
        return None
    try:
        from sonder_runtime.bootstrap.app import default_app

        request = ModelRequest(
            prompt=prompt,
            tier=model or "code",
            history=tuple(history),
            options={"stream": bool(stream)},
        )
        return default_app().session_capture_service().capture_turn(
            session_id,
            turn_id,
            request,
            request_id=request_id,
            user_message=prompt,
            model_response=content,
        )
    except Exception as error:
        _serve_logger.error(f"live session capture failed for session_id={session_id!r}, request_id={request_id!r}", exc_info=True)
        raise _LiveSessionCaptureFailure from error
_MAX_JOB_CANCEL_REASON = 256
_MAX_JOB_ID_LENGTH = 128
_MAX_JOB_KIND_LENGTH = 64
_MAX_JOB_OPERATION_LENGTH = 128
_MAX_JOB_IDEMPOTENCY_LENGTH = 256
_MAX_JOB_PARENT_LENGTH = 128


class _DuplicateJsonObjectKey(ValueError):
    """A request JSON object repeated a member name.

    JSON decoders conventionally accept repeated object members and silently
    keep the final value.  That is unsafe at an HTTP trust boundary: a proxy,
    audit record, or future validator could observe a different occurrence
    from the application.  Reject them before request routing instead.
    """


def _reject_duplicate_json_object_key(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise _DuplicateJsonObjectKey("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite_json_number(value):
    """Keep the HTTP boundary to standard finite JSON numbers."""
    raise ValueError("non-finite JSON number is not allowed")


def _local_server_log_tail():
    """Return a bounded, redacted tail for the loopback-only diagnostics page."""
    path = Path(runtime_paths.default_home()) / "run" / "sonder_serve.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - _LOCAL_LOG_TAIL_BYTES))
            raw = stream.read(_LOCAL_LOG_TAIL_BYTES)
    except OSError:
        return "(server log is not available yet)"
    # Keep the diagnostic projection stable across Git/OS newline modes;
    # carriage returns are transport framing, not control characters to mask.
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if size > len(raw):
        text = "(showing the latest %d KiB)\n%s" % (_LOCAL_LOG_TAIL_BYTES // 1024, text)
    # This page is intentionally read-only and loopback-only, but its content
    # is still browser-visible diagnostic data.  Do not keep a smaller,
    # drift-prone log-only secret matcher here: use the same conservative
    # projection that protects activity output (quoted assignments, bearer
    # credentials, URI credentials, JWTs, and recognizable provider keys).
    return activity_tracker._redact_text(text)


_LOCAL_LOG_PAGE = """<!doctype html>
<html lang=\"en\"><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">
<title>Sonder local server</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#07121f;color:#d9efff;font:14px ui-monospace,Consolas,monospace}header{padding:16px 22px;background:#0b2b4a;color:#75cfff;font-weight:700;display:flex;gap:12px;align-items:center;flex-wrap:wrap}main{padding:16px 22px}small{color:#b7cad8;font-weight:400}.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}button{background:#123e62;color:#e8f6ff;border:1px solid #2f739f;border-radius:5px;padding:8px 12px;font:inherit;cursor:pointer}button:hover,button:focus-visible{background:#19547e;outline:2px solid #75cfff;outline-offset:2px}label{padding:8px 4px}pre{white-space:pre-wrap;word-break:break-word;background:#050b12;border:1px solid #27658f;padding:14px;min-height:60vh;max-height:72vh;overflow:auto;line-height:1.45}pre:focus-visible{outline:2px solid #75cfff;outline-offset:2px}.offline{color:#ffca80}
</style></head><body>
<header><span>Sonder local server</span><small id=\"state\" role=\"status\" aria-live=\"polite\">Connecting</small></header>
<main><p>Live, read-only server-log tail. This page is available only from loopback.</p>
<div class=\"controls\" aria-label=\"Log controls\"><button id=\"pause\" type=\"button\" aria-pressed=\"false\">Pause updates</button><button id=\"refresh\" type=\"button\">Refresh now</button><button id=\"copy\" type=\"button\">Copy visible log</button><label><input id=\"follow\" type=\"checkbox\" checked> Follow latest output</label></div>
<pre id=\"log\" tabindex=\"0\" aria-label=\"Redacted server log\" aria-busy=\"true\">Loading...</pre></main>
<script>
const log=document.getElementById('log'),state=document.getElementById('state'),pause=document.getElementById('pause'),refreshNow=document.getElementById('refresh'),copy=document.getElementById('copy'),follow=document.getElementById('follow');
const POLL_MS=1000,MAX_BACKOFF_MS=30000;let timer=0,paused=false,inFlight=false,failures=0;
function setState(text,offline=false){state.textContent=text;state.classList.toggle('offline',offline)}
function schedule(delay=POLL_MS){clearTimeout(timer);timer=setTimeout(refresh,delay)}
async function refresh(){
  if(paused||document.hidden){schedule();return}
  if(inFlight)return
  inFlight=true;log.setAttribute('aria-busy','true');const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),5000);
  try{
    const response=await fetch('/v1/local/server-log',{cache:'no-store',signal:controller.signal,headers:{Accept:'application/json'}});
    if(!response.ok)throw new Error('HTTP '+response.status)
    const payload=await response.json();if(typeof payload.log!=='string')throw new Error('invalid response')
    const wasAtEnd=log.scrollHeight-log.scrollTop-log.clientHeight<32;log.textContent=payload.log;
    if(follow.checked||wasAtEnd)log.scrollTop=log.scrollHeight
    failures=0;setState('Updated '+new Date().toLocaleTimeString())
  }catch(error){failures=Math.min(failures+1,5);setState(error.name==='AbortError'?'Update timed out':'Feed unavailable',true)
  }finally{clearTimeout(timeout);inFlight=false;log.setAttribute('aria-busy','false');schedule(Math.min(MAX_BACKOFF_MS,POLL_MS*(2**failures)))}
}
pause.addEventListener('click',()=>{paused=!paused;pause.textContent=paused?'Resume updates':'Pause updates';pause.setAttribute('aria-pressed',String(paused));setState(paused?'Updates paused':'Resuming');if(!paused)refresh()});
refreshNow.addEventListener('click',()=>{if(!inFlight)refresh()});
copy.addEventListener('click',async()=>{try{await navigator.clipboard.writeText(log.textContent||'');setState('Visible log copied')}catch(_){setState('Copy unavailable',true)}});
document.addEventListener('visibilitychange',()=>{if(!document.hidden&&!paused)refresh()});window.addEventListener('pagehide',()=>clearTimeout(timer));refresh();
</script></body></html>"""


def _request_route(path) -> str:
    """Return a normalized routing path while preserving query data elsewhere."""
    return urllib.parse.urlsplit(str(path or "")).path.rstrip("/") or "/"


def _job_record_payload(record):
    return {
        "job_id": record.identity.job_id,
        "kind": record.identity.kind,
        "operation_id": record.identity.operation_id,
        "idempotency_key": record.identity.idempotency_key,
        "parent_job_id": record.identity.parent_job_id,
        "parent_session_id": record.identity.parent_session_id,
        "status": record.status.value,
        "revision": record.revision,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
        "result": record.result,
        "error": record.error,
    }


def _job_cancel_id(path):
    prefix = "/v1/jobs/"
    suffix = "/cancel"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    encoded = path[len(prefix):-len(suffix)]
    if not encoded or "/" in encoded:
        return None
    job_id = urllib.parse.unquote(encoded)
    return job_id if job_id and "/" not in job_id else None


def _compute_job_route(path):
    """Return an exact compute-job operation and one decoded identity."""
    if path == "/v1/compute/jobs":
        return "submit", None
    prefix = "/v1/compute/jobs/"
    if not path.startswith(prefix):
        return None
    suffix = path[len(prefix):]
    idempotency_prefix = "by-idempotency/"
    if suffix.startswith(idempotency_prefix):
        encoded = suffix[len(idempotency_prefix):]
        if not encoded or "/" in encoded:
            return None
        value = urllib.parse.unquote(encoded)
        return ("by_idempotency", value) if value and "/" not in value else None
    artifact_marker = "/artifacts/"
    if artifact_marker in suffix:
        encoded_job, encoded_name = suffix.split(artifact_marker, 1)
        if not encoded_job or not encoded_name or "/" in encoded_job or "/" in encoded_name:
            return None
        job_id = urllib.parse.unquote(encoded_job)
        artifact_name = urllib.parse.unquote(encoded_name)
        if not job_id or "/" in job_id or not artifact_name:
            return None
        return "artifact", (job_id, artifact_name)
    if suffix.endswith("/cancel"):
        encoded = suffix[:-len("/cancel")]
        if not encoded or "/" in encoded:
            return None
        value = urllib.parse.unquote(encoded)
        return ("cancel", value) if value and "/" not in value else None
    if not suffix or "/" in suffix:
        return None
    value = urllib.parse.unquote(suffix)
    return ("status", value) if value and "/" not in value else None


def _job_start_payload(req):
    """Validate the bounded identity accepted by the durable job command."""
    required = {"job_id", "kind", "operation_id", "idempotency_key"}
    optional = {"parent_job_id", "parent_session_id"}
    if set(req) - required - optional or not required.issubset(req):
        raise ValueError(
            "job start requires exactly the identity fields job_id, kind, "
            "operation_id, and idempotency_key"
        )
    limits = {
        "job_id": _MAX_JOB_ID_LENGTH,
        "kind": _MAX_JOB_KIND_LENGTH,
        "operation_id": _MAX_JOB_OPERATION_LENGTH,
        "idempotency_key": _MAX_JOB_IDEMPOTENCY_LENGTH,
        "parent_job_id": _MAX_JOB_PARENT_LENGTH,
        "parent_session_id": _MAX_JOB_PARENT_LENGTH,
    }
    payload = {}
    for name, limit in limits.items():
        if name not in req:
            continue
        value = req[name]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(f"{name} must be a non-empty string of at most {limit} characters")
        if "/" in value or "\\" in value:
            raise ValueError(f"{name} must not contain path separators")
        payload[name] = value
    return payload


_JOB_STREAM_DEFAULT_EVENTS = 64
_JOB_STREAM_MAX_EVENTS = 256
_JOB_STREAM_DEFAULT_BYTES = 16 * 1024
_JOB_STREAM_MAX_BYTES = 64 * 1024
_JOB_STREAM_MAX_AFTER = (1 << 63) - 1


def _job_subroute_id(path, suffix):
    prefix = "/v1/jobs/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    encoded = path[len(prefix):-len(suffix)]
    if not encoded or "/" in encoded:
        return None
    job_id = urllib.parse.unquote(encoded)
    return job_id if job_id and "/" not in job_id else None


def _job_record_id(path):
    """Return one safely encoded job id from the direct read route."""
    prefix = "/v1/jobs/"
    if not path.startswith(prefix):
        return None
    encoded = path[len(prefix):]
    if not encoded or "/" in encoded:
        return None
    job_id = urllib.parse.unquote(encoded)
    return job_id if job_id and "/" not in job_id else None


def _job_stream_query(raw_query):
    query = urllib.parse.parse_qs(raw_query, keep_blank_values=True)

    def bounded(name, default, lower, upper):
        values = query.get(name)
        if not values:
            return default
        if len(values) != 1 or not values[0]:
            raise ValueError(f"{name} must be an integer between {lower} and {upper}")
        try:
            value = int(values[0])
        except (TypeError, ValueError):
            raise ValueError(
                f"{name} must be an integer between {lower} and {upper}"
            ) from None
        if value < lower or value > upper:
            raise ValueError(f"{name} must be an integer between {lower} and {upper}")
        return value

    return (
        bounded("after", 0, 0, _JOB_STREAM_MAX_AFTER),
        bounded("max_events", _JOB_STREAM_DEFAULT_EVENTS, 1, _JOB_STREAM_MAX_EVENTS),
        bounded("max_bytes", _JOB_STREAM_DEFAULT_BYTES, 1, _JOB_STREAM_MAX_BYTES),
    )


def _job_output_event_payload(event):
    spill = event.spill
    return {
        "sequence": event.watermark.sequence,
        "stream": event.stream.value,
        "data": event.data,
        "spill": None if spill is None else {
            "digest": spill.digest,
            "preview": spill.preview,
            "size": spill.size,
            "mime_type": spill.mime_type,
            "owner_id": spill.owner_id,
        },
    }


def _local_log_dashboard_allowed(peer: str) -> bool:
    """Whether unauthenticated browser log diagnostics are safe to expose.

    A reverse proxy commonly connects to the loopback listener itself. Do not
    let that topology turn this convenience page into remote, unauthenticated
    observability; authenticated API diagnostics remain separate.
    """
    return not TLS_TERMINATED_BY_PROXY and _is_loopback_host(peer)


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
            _serve_logger.critical(f"SONDER_AUTH_MODE has invalid value={configured!r}, cannot start server")
            raise RuntimeError("invalid SONDER_AUTH_MODE")
        _serve_logger.info(f"Auth mode resolved, mode={mode!r} (explicit)")
        _serve_logger.debug(f"_resolve_auth_mode: explicit mode={mode!r}")
        return mode
    if not api_key and not require_account:
        _serve_logger.warning("auth mode defaulting to local-open: no API key or account requirement configured")
    if api_key:
        _serve_logger.debug("_resolve_auth_mode: inferred api-key mode from API key presence")
        return "api-key"
    if require_account:
        _serve_logger.debug("_resolve_auth_mode: inferred account mode from require_account")
        return "account"
    _serve_logger.debug("_resolve_auth_mode: defaulting to local-open")
    return "local-open"


def _parse_cors_origins(value):
    return frozenset(
        origin.strip()
        for origin in (value or "").split(",")
        if origin.strip() and origin.strip() != "*"
    )

# Auth + bind config. No credentials remains open only on loopback.
API_KEY = os.environ.get("SONDER_API_KEY", "")
AUTH_SECRET = os.environ.get("SONDER_AUTH_SECRET", "")
HOST = os.environ.get("SONDER_HOST", "127.0.0.1")
REQUIRE_ACCOUNT = _env_flag("SONDER_REQUIRE_ACCOUNT")
AUTH_MODE = _resolve_auth_mode(API_KEY, REQUIRE_ACCOUNT)
CORS_ORIGINS = _parse_cors_origins(os.environ.get("SONDER_CORS_ORIGINS", ""))
# The validated serve entry point sets this whenever a TLS-terminating proxy
# fronts the otherwise-loopback runtime. Peer-address checks alone cannot tell
# that proxy apart from a direct local browser.
TLS_TERMINATED_BY_PROXY = _env_flag("SONDER_TLS_TERMINATED_BY_PROXY")
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
# Bound blocking SSE writes independently of header/body reads.  The stream is
# emitted only after generation has completed, so this never shortens a model
# call; it limits an idle client that stops accepting the response.
STREAM_IDLE_TIMEOUT_SECONDS = max(1, _env_int(
    "SONDER_STREAM_IDLE_TIMEOUT_SECONDS", REQUEST_TIMEOUT_SECONDS
))
# Several routes answer before the request body is read: the origin rejection,
# the framing and media-type errors, the oversized-body refusal, the
# authentication-failure limiter, and /v1/admin/drain. A response that skips the
# body must not leave those bytes on a connection that stays open, or the peer's
# next request is parsed starting inside them. A small, fully framed body is
# taken off the socket instead so HTTP/1.1 reuse survives; anything larger keeps
# the 413 bound meaningful and ends the connection (RFC 9112 6.3). The read is
# given its own short deadline so an error path never inherits the full
# per-connection wait above.
MAX_DISCARDED_BODY_BYTES = min(MAX_REQUEST_BYTES, 64 * 1024)
DISCARD_BODY_TIMEOUT_SECONDS = 5
# Closing a socket whose receive buffer still holds body bytes makes the OS
# reset the connection, and the reset discards the response already written --
# the caller would see a dropped connection instead of the 413 that explains
# the refusal. Soak what is already in flight for a short, bounded window
# first (RFC 9112 9.6, lingering close). Both bounds are deliberately small:
# this runs only on connections that are ending anyway.
LINGERING_CLOSE_SECONDS = 1.0
LINGERING_CLOSE_BYTES = 256 * 1024
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


def _extension_authority(context):
    """Bind extension authority to an already authenticated administrator."""
    account = context.get("account")
    if isinstance(account, dict):
        actor = account.get("username") or account.get("id") or "authenticated-admin"
    else:
        actor = account or "authenticated-admin"
    return ExtensionAuthority(
        actor=str(actor),
        operations=frozenset({
            "registry_health", "inspect", "define", "define_installed", "start", "stop", "delete",
        }),
    )


def _is_extension_route(path):
    return path == "/v1/extensions" or path.startswith("/v1/extensions/")


def _is_trace_projection_route(path):
    return path == "/v1/observability/trace"


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
    # A request pins its selected retained state before lifecycle admission.
    # The state lock itself is acquired only after admission, so it is not a
    # sufficient indication that a queued request still owns this entry.
    session_pins: int = 0


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
    # Deterministic request-cache consultation result: "hit", "miss", or ""
    # when the turn never consulted the cache.  A closed set by construction,
    # so it is safe to surface in the bounded HTTP receipt.
    cache: str = ""


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
HTTP_SESSION_STATE_LIMIT = max(2, min(
    1024, _env_int("SONDER_HTTP_SESSION_STATE_LIMIT", 128)
))
# Admission cap for one hosted account inside the shared state map.  The map's
# global LRU alone lets a single account cycling fresh session names evict
# every other account's live conversation state (trace/strict toggles, /run
# source, debug events); at this cap an account recycles its own oldest idle
# entry instead.  Single-principal surfaces (local-open, api-key) keep the
# global bound: there is no other tenant to protect there.
HTTP_SESSION_STATE_OWNER_LIMIT = max(1, min(
    HTTP_SESSION_STATE_LIMIT - 1,
    _env_int("SONDER_HTTP_SESSION_STATE_OWNER_LIMIT", 32),
))


def configure_typed_config(config) -> None:
    """Bind validated ``SonderConfig`` values at the HTTP boundary."""
    _serve_logger.debug("configure_typed_config: binding server config to HTTP boundary")
    _serve_logger.info(f"Applying typed server configuration, host={config.server.host!r}, port={config.server.port}, auth_mode={config.server.auth_mode!r}")
    global CONFIGURED_PORT, API_KEY, AUTH_SECRET, HOST, REQUIRE_ACCOUNT, AUTH_MODE, CORS_ORIGINS
    global TLS_TERMINATED_BY_PROXY, ALLOW_REGISTRATION, MAX_REQUEST_BYTES
    global MAX_DISCARDED_BODY_BYTES, REQUEST_TIMEOUT_SECONDS
    global STREAM_IDLE_TIMEOUT_SECONDS, HTTP_SESSION_STATE_LIMIT
    global HTTP_SESSION_STATE_OWNER_LIMIT, TRAIN_MAX_N

    server_config = config.server
    from sonder_runtime.adapters.web import listener_probe
    listener_probe.configure_typed_config(config)
    CONFIGURED_PORT = server_config.port
    API_KEY = config.secrets.api_key
    AUTH_SECRET = config.secrets.auth_secret
    HOST = server_config.host
    REQUIRE_ACCOUNT = server_config.require_account
    AUTH_MODE = server_config.auth_mode
    if AUTH_MODE == "api-key" and not API_KEY and _is_loopback_host(HOST):
        _serve_logger.warning("api-key auth mode downgraded to local-open: no API key configured on loopback bind")
        AUTH_MODE = "local-open"
    CORS_ORIGINS = frozenset(server_config.cors_origins)
    TLS_TERMINATED_BY_PROXY = server_config.tls_terminated_by_proxy
    ALLOW_REGISTRATION = server_config.allow_registration
    MAX_REQUEST_BYTES = max(1, min(16 * 1024 * 1024, server_config.max_request_bytes))
    MAX_DISCARDED_BODY_BYTES = min(MAX_REQUEST_BYTES, 64 * 1024)
    REQUEST_TIMEOUT_SECONDS = max(5, server_config.request_timeout_seconds)
    STREAM_IDLE_TIMEOUT_SECONDS = max(1, server_config.stream_idle_timeout_seconds)
    HTTP_SESSION_STATE_LIMIT = max(2, min(1024, server_config.session_state_limit))
    HTTP_SESSION_STATE_OWNER_LIMIT = max(
        1, min(HTTP_SESSION_STATE_LIMIT - 1, server_config.session_state_owner_limit)
    )
    TRAIN_MAX_N = max(1, server_config.train_max_n)
    global _HEALTH_STATUS_FACADE, _TRUSTED_PROXY_NETWORKS
    _HEALTH_STATUS_FACADE = HealthStatusFacade(
        metrics_path=config.observability.metrics_path,
    )
    _TRUSTED_PROXY_NETWORKS = tuple(
        ipaddress.ip_network(cidr, strict=False)
        for cidr in server_config.trusted_proxy_cidrs
    )
_HTTP_SESSION_STATES = OrderedDict()
_HTTP_SESSION_STATES_LOCK = threading.RLock()


def _state_or_legacy(state):
    return state if state is not None else _LEGACY_STATE


def _state_principal(context):
    context = context or {}
    account = context.get("account") or {}
    if account:
        identity = _account_identity(account)
        if not identity:
            raise PermissionError("authenticated account identity is unavailable")
        return "account:%s" % identity
    if context.get("api_key"):
        return "api-key"
    return "local-open"


def _account_identity(account) -> str:
    """Return one bounded identity or ``""`` for malformed auth output."""
    if not isinstance(account, dict):
        return ""
    identity = account.get("username") or account.get("id") or ""
    if not isinstance(identity, str):
        return ""
    identity = identity.strip()
    if (
        not identity
        or len(identity) > 128
        or any(ord(char) < 32 or ord(char) == 127 for char in identity)
    ):
        return ""
    return identity


def _request_idempotency_key(context, endpoint, supplied_key):
    """Return an opaque idempotency key bound to one HTTP principal.

    Idempotency keys are client-chosen and the lifecycle cache is process-wide.
    Reusing a key across two hosted accounts must not replay one administrator's
    response to another, even when they happen to choose the same key.  Hashing
    also keeps the cache bounded by a fixed-size key rather than retaining a
    potentially large HTTP header verbatim.  A missing key preserves the
    lifecycle's existing non-idempotent behavior.
    """
    supplied_key = str(supplied_key or "").strip()
    if not supplied_key:
        return ""
    material = "http-idempotency\0%s\0%s\0%s" % (
        str(endpoint or ""), _state_principal(context), supplied_key,
    )
    return "hi-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _http_action_idempotency_key(context, supplied_key, action):
    """Return a principal- and action-bound replay key for HTTP work controls.

    HTTP clients commonly retry after a connection disappears while a long
    agent/autopilot request is still running.  A bare client key is not a safe
    cache key: two accounts can choose the same value, and the same client can
    accidentally reuse it for a different operation.  Hash the opaque client
    value together with the authenticated principal and the exact canonical
    control text before handing it to the process-local lifecycle coordinator.
    Nothing secret, account-controlled, or request text is retained in the
    coordinator's bounded idempotency map.
    """
    key = str(supplied_key or "").strip()
    if not key:
        return ""
    # HTTP headers are already bounded by the server, but keep this helper's
    # behavior explicit when it is called directly in tests or embeddings.
    if len(key) > 512:
        return ""
    material = "\0".join((
        "served-action-idempotency-v1",
        _state_principal(context),
        key,
        str(action or ""),
    ))
    return "act-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _receipt_owner_scope(context):
    """Opaque per-principal budget key for durable action receipts.

    The receipt store never holds account identity; like the fanout and task
    scopes above, a domain-separated digest of the principal gives it a stable
    counter key so one account's distinct Idempotency-Keys draw down that
    account's admission budget instead of the shared store.
    """
    material = "served-receipt-owner\0" + _state_principal(context)
    return "rw-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _idempotent_http_action(context, supplied_key, action, factory):
    """Run an opt-in action once, preserving uncertainty across restarts."""
    cache_key = _http_action_idempotency_key(context, supplied_key, action)
    if not cache_key:
        return factory()

    def durable_factory():
        try:
            state = served_action_receipts.claim(
                cache_key, owner_scope=_receipt_owner_scope(context)
            )
        except (OSError, sqlite3.Error, ValueError):
            # An explicit replay key promises no duplicate side effect.  If its
            # durable guard is unavailable, refusing is safer than executing a
            # long-running mutation without a recoverable receipt.
            _serve_logger.error(f"idempotency receipt store unavailable for cache_key={cache_key!r}, refusing action", exc_info=True)
            refusal = (
                "idempotency receipt unavailable: the action was not started. "
                "Retry after restoring local runtime storage."
            )
            return refusal
        if state == served_action_receipts.REJECTED:
            # Admitting this new key would exceed the durable receipt budget
            # (global or this principal's).  Refusing is deterministic
            # backpressure: running without a receipt would silently drop the
            # no-duplicate promise the client asked for.
            _serve_logger.warning(f"idempotency receipt capacity exhausted for cache_key={cache_key!r}")
            refusal = (
                "idempotency receipt capacity exhausted: the action was not "
                "started. Reuse the Idempotency-Key of the retried action, or "
                "retry after older completed receipts expire."
            )
            return refusal
        if state == "completed":
            refusal = (
                "idempotent action refused: it already completed before the "
                "current server process. It was not run again; query its "
                "status or submit a new action with a new Idempotency-Key."
            )
            return refusal
        if state in {"started", "uncertain"}:
            _serve_logger.warning(f"idempotent action refused with uncertain prior outcome, cache_key={cache_key!r}, state={state!r}")
            refusal = (
                "idempotent action refused: it has an uncertain prior outcome "
                "after an interrupted server process. It was not run again; "
                "inspect the affected project/status before submitting a new action."
            )
            return refusal
        try:
            result = factory()
        except BaseException:
            # An exception after tool admission is not proof of no side
            # effect. Preserve the receipt as uncertain so the next process
            # cannot blindly replay it.
            _serve_logger.error(f"idempotent action raised after admission, marking uncertain, cache_key={cache_key!r}", exc_info=True)
            with contextlib.suppress(OSError, sqlite3.Error):
                served_action_receipts.finish(cache_key, uncertain=True)
            raise
        try:
            served_action_receipts.finish(cache_key)
        except (OSError, sqlite3.Error):
            # The side effect returned but its terminal record did not commit.
            # Leaving `started` is intentionally conservative on retry.
            _serve_logger.error(f"idempotency receipt finish failed for cache_key={cache_key!r}, receipt left as started", exc_info=True)
        return result

    return sonder_lifecycle.get().idempotent(
        cache_key,
        durable_factory,
        # The in-process result cannot outlive the durable receipt: otherwise
        # an expired key remains silently cached until unrelated cache churn.
        cache_ttl_seconds=served_action_receipts.completed_ttl_seconds(),
        # Capacity rejection writes no durable receipt and explicitly invites
        # retry after pressure drops, so never freeze that refusal in memory.
        cache_result=lambda result: not (
            isinstance(result, str)
            and result.startswith("idempotency receipt capacity exhausted:")
        ),
    )


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
    "task_plan", "task_progress", "task_ledger", "task_depend", "checklist_create",
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

# The legacy memory database predates hosted accounts.  Sessions and projects
# supplied through /v1/chat/completions are principal-namespaced before they
# reach it, but these direct tools still read or mutate global lesson,
# interaction, preference, evaluation, and training state.  Letting an
# authenticated account reach one would either disclose another account's
# material or let it influence the shared learning corpus.  Direct MCP and a
# loopback local-open deployment deliberately retain the complete single-user
# surface; hosted deployments need a first-class per-account memory store
# before these operations can safely be enabled.
_ACCOUNT_GLOBAL_MEMORY_TOOLS = frozenset((
    "apply_learned",
    "evaluation_history_status",
    "learn_from_example",
    "learning_health_status",
    "memory_embedding_backfill",
    "memory_export",
    "memory_interaction_embedding_backfill",
    "memory_privacy_review",
    "memory_quality_report",
    "memory_search",
    "recall",
    "record_outcome",
    "sonder_forget_fact",
    "session_export",
    "sonder_remember_fact",
    "sonder_sessions",
    "sonder_stats",
))


def _account_global_memory_refusal(tool, context):
    """Refuse legacy global-memory tools for a hosted account.

    This guard intentionally lives at the HTTP boundary rather than relying on
    each legacy tool to remember account authorization.  The tools do not
    accept an account scope, so passing caller-provided IDs or treating a
    project name as an authorization boundary would be a false isolation
    guarantee.
    """
    if not (context or {}).get("account"):
        return ""
    if str(tool or "").lstrip("/") not in _ACCOUNT_GLOBAL_MEMORY_TOOLS:
        return ""
    return (
        "hosted account memory is isolated to chat sessions and projects; "
        "this global memory tool is local-operator only"
    )


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


def _reasoning_request_owner(context):
    """Opaque principal bound to private reasoning captured for this request."""
    if not server._deployment_authenticates_callers():
        return ""
    material = "reasoning-owner\0" + _state_principal(context)
    return "ro-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _admission_request_owner(context):
    """Opaque principal for per-owner admission fairness accounting.

    Only in-memory in the lifecycle's in-flight map, never persisted and never
    a metric label.  Local-open deployments and pure API-key deployments
    return "": each has one indistinguishable principal, so only global
    bounds apply.  Account-bearing modes retain an opaque owner key so one
    account cannot consume every shared slot.
    """
    if (not server._deployment_authenticates_callers()
            or context.get("mode") == "api-key"):
        return ""
    material = "admission-owner\0" + _state_principal(context)
    return "ao-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _request_cache_scope(context):
    """Opaque request-owner scope for the deterministic request cache.

    Binds cache entries to one HTTP principal so two accounts can never share
    a response, even for byte-identical requests.  Domain separated from the
    fanout/reasoning owner digests so a value from one namespace can never be
    replayed in another, and hashed so the raw principal is never stored as a
    cache-key input.
    """
    material = "request-cache-owner\0" + _state_principal(context)
    return "qc-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


def _feed_request_owner(context):
    """Opaque principal that scopes the live execution feed to one caller.

    Unauthenticated deployments have a single trusted local operator and no
    second party to protect, so the key collapses to the unowned/local domain.
    """
    if not server._deployment_authenticates_callers():
        return ""
    material = "live-feed-owner\0" + _state_principal(context)
    return "lf-" + hashlib.sha256(material.encode("utf-8")).hexdigest()


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
        if candidate.session_pins == 0 and candidate.lock.acquire(blocking=False):
            try:
                _HTTP_SESSION_STATES.pop(key, None)
            finally:
                candidate.lock.release()


def _evict_owner_lru_session_state(principal):
    """Drop `principal`'s least-recent idle state; False if all are in-flight."""
    for key, candidate in list(_HTTP_SESSION_STATES.items()):
        if key[0] != principal:
            continue
        if candidate.session_pins == 0 and candidate.lock.acquire(blocking=False):
            try:
                _HTTP_SESSION_STATES.pop(key, None)
            finally:
                candidate.lock.release()
            return True
    return False


def _acquire_http_conversation_state(context, session, token="", *, pin=False):
    """Atomically select a bounded state and optionally pin it for a request.

    Retention is bounded twice: globally by HTTP_SESSION_STATE_LIMIT, and per
    hosted account by HTTP_SESSION_STATE_OWNER_LIMIT so one account's session
    churn recycles its own oldest idle entry rather than evicting another
    account's live conversation.  When no entry can be recycled without
    touching an in-flight lock or another principal, admission fails closed to
    request-local state.
    """

    session = (session or "").strip()
    if not session:
        return ConversationState(token=token or "", account=context.get("account")), False
    principal = _state_principal(context)
    key = (principal, session)
    with _HTTP_SESSION_STATES_LOCK:
        state = _HTTP_SESSION_STATES.get(key)
        if state is None:
            if context.get("account"):
                owned = sum(
                    1 for existing in _HTTP_SESSION_STATES
                    if existing[0] == principal
                )
                while owned >= HTTP_SESSION_STATE_OWNER_LIMIT:
                    if not _evict_owner_lru_session_state(principal):
                        # Every retained conversation for this account is
                        # mid-request.  Stay at the cap and use request-local
                        # state rather than growing or evicting another
                        # account's entry.
                        _serve_logger.warning(f"session state owner limit reached: all {owned} sessions for principal are in-flight, falling back to request-local state")
                        return ConversationState(
                            token=token or "", account=context.get("account")
                        ), False
                    owned -= 1
            _prune_http_session_states(HTTP_SESSION_STATE_LIMIT - 1)
            if len(_HTTP_SESSION_STATES) >= HTTP_SESSION_STATE_LIMIT:
                # All retained conversations are active. Stay bounded and use
                # request-local state rather than evicting an in-flight lock.
                _serve_logger.warning(f"global session state limit reached: {len(_HTTP_SESSION_STATES)}/{HTTP_SESSION_STATE_LIMIT} sessions all in-flight, falling back to request-local state")
                return ConversationState(
                    token=token or "", account=context.get("account")
                ), False
            state = ConversationState()
            _HTTP_SESSION_STATES[key] = state
        _HTTP_SESSION_STATES.move_to_end(key)
        if token:
            state.token = token
        if context.get("account"):
            state.account = context["account"]
        if pin:
            state.session_pins += 1
        return state, bool(pin)


def _http_conversation_state(context, session, token=""):
    """Return bounded per-principal state without retaining a request pin."""
    return _acquire_http_conversation_state(context, session, token)[0]


def _release_http_conversation_state(state, pinned):
    """Release a request's eviction pin after its state-dependent work ends."""
    if not pinned:
        return
    with _HTTP_SESSION_STATES_LOCK:
        state.session_pins = max(0, int(state.session_pins or 0) - 1)


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
    # ``tool_contract`` is deliberately absent: this module binds the typed
    # ``authority_contract`` under that name (pure, explicit inputs), and the
    # root ``tool_contract`` module has a different call shape. Reloading the
    # root module here once rebound the served gate to it mid-process, so an
    # authority edit could leave two contracts live and the gate calling one
    # with the other's arguments. The root module still reloads in ``server``.
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
    if not token:
        return None
    return _legacy_runtime()._admin_account_from_token(token)


def _effective_auth_mode():
    if REQUIRE_ACCOUNT and not API_KEY and AUTH_MODE in ("local-open", "api-key"):
        return "account"
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
    # Authentication adapters are trusted code, but their output still forms
    # every session/cache/receipt namespace.  A malformed account must not be
    # accepted and collapsed into a shared "unknown" principal.
    if account is not None and not _account_identity(account):
        account = None
    authorized = {
        "api-key": api_key_ok,
        "account": account is not None,
        "both": api_key_ok and account is not None,
        "either": api_key_ok or account is not None,
        "local-open": True,
    }[mode]
    _serve_logger.debug(f"_auth_context: mode={mode!r}, authorized={authorized}, api_key_ok={api_key_ok}, has_account={account is not None}")
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
    "permission_approve": "permission_rule_change",
    "elevate": "permission_mode_change",
    "runtime_policy_update": "runtime_policy_change",
    "runtime_source_update": "selfmod_deploy",
    "runtime_source_stash": "selfmod_deploy",
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
    # The workflow store is process-wide.  Listing it reveals every saved
    # action payload and description, which can include repository paths,
    # command arguments, and operator-authored context.  It is therefore not
    # an ordinary account's read-only status view on a shared deployment.
    "workflow_list": "automation_lifecycle",
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


def _http_system_operation_for(tool):
    return tool_contract.system_operation_for(
        tool,
        operation_tools=SYSTEM_OPERATION_TOOLS,
        operator_tools=getattr(server, "_AGENT_SYSTEM_OPERATOR_TOOLS", ()),
        canonicalize=getattr(server, "_canonical_agent_tool_name", None),
    )


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


def _a2a_discovery_base_url():
    """Resolve the safe local A2A URL used by agent-card discovery.

    A workstation listener already has an unambiguous loopback address. Keep
    proxy/non-loopback deployments explicit so discovery never invents an
    externally reachable scheme or authority.
    """
    configured = os.environ.get("SONDER_A2A_BASE_URL", "").strip()
    if configured:
        return configured
    if _is_loopback_host(HOST):
        return "http://%s:%d" % (HOST, CONFIGURED_PORT)
    _serve_logger.warning(f"A2A discovery base URL cannot be resolved: non-loopback host={HOST!r} and SONDER_A2A_BASE_URL is not set")
    return ""


def _selected_listener_port(config=None, argv=None):
    """Resolve the port the direct entrypoint will actually bind."""
    port = DEFAULT_PORT if config is None else CONFIGURED_PORT
    arguments = sys.argv if argv is None else argv
    if len(arguments) > 1:
        try:
            port = int(arguments[1])
        except (TypeError, ValueError):
            pass
    elif config is None:
        port = int(os.environ.get("SONDER_PORT", DEFAULT_PORT))
    return port


def _http_server_location_lookup_allowed(context):
    """Allow server-IP location lookup only for genuinely local-open use."""
    return (
        isinstance(context, dict)
        and context.get("mode") == "local-open"
        and _is_loopback_host(HOST)
    )


def _validate_bind_security(
    host,
    api_key=None,
    auth_mode=None,
    auth_secret=None,
    tls_terminated_by_proxy=None,
):
    _serve_logger.debug(f"_validate_bind_security: host={host!r}, auth_mode={auth_mode!r}, tls_proxy={tls_terminated_by_proxy}")
    _serve_logger.info(f"Validating bind security, host={host!r}")
    # Unsafe lab acknowledgement tightens exposure: unlike normal served mode,
    # there is deliberately no authenticated non-loopback topology available.
    unsafe_lab.require_startup(host=host)
    api_key = API_KEY if api_key is None else api_key
    mode = _effective_auth_mode() if auth_mode is None else auth_mode
    auth_secret = AUTH_SECRET if auth_secret is None else auth_secret
    if mode == "api-key" and not api_key:
        _serve_logger.critical(f"bind security validation failed: api-key auth mode requires SONDER_API_KEY, host={host!r}")
        raise RuntimeError("api-key auth mode requires SONDER_API_KEY")
    if mode == "both" and (not api_key or not auth_secret):
        _serve_logger.critical(f"bind security validation failed: 'both' auth mode requires API key and account auth secret, host={host!r}, has_api_key={bool(api_key)}, has_auth_secret={bool(auth_secret)}")
        raise RuntimeError("both auth mode requires API key and account auth secret")
    if _is_loopback_host(host):
        return
    _serve_logger.warning(f"non-loopback bind requested, host={host!r}: verifying TLS proxy and strong auth requirements")
    # ``sonder_runtime serve`` reaches this module after validating the typed
    # config.  This script is also a supported direct entrypoint, though, so it
    # must not silently turn ``SONDER_HOST=0.0.0.0`` plus a key into a plaintext
    # public listener.  The exported declaration is intentionally checked at
    # the last responsible moment, immediately before ``serve_forever`` can
    # bind.  An operator who uses the direct entrypoint must make the same
    # explicit reverse-proxy assertion as a configured deployment.
    if tls_terminated_by_proxy is None:
        tls_terminated_by_proxy = _env_flag("SONDER_TLS_TERMINATED_BY_PROXY")
    if not tls_terminated_by_proxy:
        _serve_logger.critical(f"bind security violation: non-loopback host={host!r} without TLS proxy declaration, refusing to start")
        raise RuntimeError(
            "non-loopback bind requires SONDER_TLS_TERMINATED_BY_PROXY=1 "
            "for a TLS-terminating reverse proxy"
        )
    # The same policy sonder_config.validate enforces, read from the same
    # constant. It was restated here as a bare 24, so raising the named
    # MIN_API_KEY_LENGTH -- the obvious single-point edit -- would have
    # hardened the config validator and left the actual bind-time gate behind.
    strong_api = len(api_key) >= runtime_config.MIN_API_KEY_LENGTH
    strong_account = len(auth_secret) >= MIN_ACCOUNT_SECRET_LENGTH
    secure = {
        "api-key": strong_api,
        "account": strong_account,
        "both": strong_api and strong_account,
        "either": strong_api and strong_account,
        "local-open": False,
    }.get(mode, False)
    if not secure:
        _serve_logger.critical(f"bind security violation: non-loopback host={host!r} with auth_mode={mode!r} lacks strong credentials, refusing to start")
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
    "/inspectimage", "/vision", "/analyzeimage", "/vision_analyze",
    "/mkdir", "/runprogram", "/runscript",
    "/privacy", "/privacyreview", "/privacyfix", "/embeddings", "/embedfix",
    "/capacity", "/agentcapacity", "/agentcancel", "/cancelagents",
    "/agentretry", "/retryagent",
    "/runtime", "/models",
    "/update", "/updatecheck", "/updatesource", "/stash", "/runtime-stash",
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
    if command in ("/stash", "/runtime-stash"):
        action = pieces[1].lower() if len(pieces) > 1 else "status"
        return action in ("save", "save-untracked", "pop")
    if command in ("/selfmod", "/selfmodify"):
        action = pieces[1].lower() if len(pieces) > 1 else "status"
        return action not in ("status", "show", "list", "history", "inspect", "diff", "tests", "backups", "verify-backup", "opportunities", "help", "?")
    return command in DANGEROUS_HTTP_SLASH_COMMANDS


# Slash names gated by action rather than by membership in the frozenset above.
_CONDITIONALLY_GATED_SLASH = frozenset({
    "/autopilot", "/auto", "/runtime", "/models", "/update", "/updatecheck", "/updatesource", "/stash", "/runtime-stash", "/selfmod", "/selfmodify",
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
    value = str(text or "")
    idx = value.find(server.FOOTER_PREFIX)
    if idx == -1:
        return server._strip_activity_block(value)
    # ``server.with_footer`` appends the activity block before its invisible
    # interaction token. Terminal users receive that human-readable evidence;
    # OpenAI-compatible `message.content` must remain only the model answer.
    return server._strip_activity_block(value[:idx])


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
    global grounding, training_tasks, intents, feedback, admin_auth, debug_dump
    modules = live_reload.reload_changed_modules(LIVE_RELOAD_MODULES)
    if modules:
        _serve_logger.info(f"Live-reloaded modules: {', '.join(sorted(modules))}")
    if "server" in modules:
        configure_legacy_runtime(modules["server"])
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
    _server_side_history().  Sonder-generated assistant decoration is not
    conversational content, however: older HTTP replies can contain an
    observable activity block, trace, or interaction footer.  Normalize only
    those assistant turns just as the server-side replay path does, so a
    client that faithfully resends prior API responses does not feed telemetry
    back to the model on a concise follow-up.
    """
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
        if role == "assistant":
            content = server._strip_activity_block(_answer_only(content)).strip()
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
        runtime_paths.default_home(),
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

    `interactive=False`, like every other caller with nobody to prompt: file
    changes, host programs and destructive tools are refused with the
    remedies named, ask-class tools proceed on the record, and a `deny` rule
    and `plan` refuse. Only a `deny` can come back from `decide()` here,
    which is why this is a flat loop rather than a copy of the console's
    ask-and-rank gate.

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
    # that it will take the read-only branch; the rules live in the catalog so
    # this chain and `server.control_command` cannot disagree about a read.
    tools = command_catalog.narrow_branch_tools(cmd, argument, tools)
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
        operation = _http_system_operation_for(
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


def _http_tool_refusal(tools, label, context=None, arguments=None):
    """The decision itself, shared by this surface's two entry points.

    ``arguments`` are meaningful only for a single tool (the dynamic
    ``/<tool>`` path passes its parsed keywords): they let an unattended
    refusal name the call so an operator can approve exactly it once, and
    let such an approval answer the retry.

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
        memory_error = _account_global_memory_refusal(tool, context)
        if memory_error:
            return "refused %s: %s" % (label, memory_error)
        operation = _http_system_operation_for(tool)
        if operation and context is not None:
            if operation == tool_contract.SYSTEM_OPERATION_UNBOUND:
                # Durable-authority tools are already refused non-interactively
                # by decide() below -- on every surface, for every role -- and
                # its refusal names the remedy (the console prompt or an
                # explicit allow rule). Returning the generic unbound message
                # here would replace actionable text with a role demand that
                # even an admin cannot satisfy on this path.
                if permission_policy.is_durable_authority_tool(tool):
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
        decision = permission_policy.decide_for_caller(
            tool, interactive=False, gate_control_exempt=True, surface="http",
            arguments=arguments if (len(tools) == 1 and isinstance(arguments, dict)) else None,
        )
        if decision is None:
            continue
        if decision.action == "deny":
            return "refused %s: %s (mode: %s)" % (
                label, decision.reason,
                permission_policy.mode_label(decision.mode),
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
    if command in ("/stash", "/runtime-stash") and action in ("save", "save-untracked", "pop"):
        return "selfmod_deploy"
    return ""


def _handle_slash(content, messages=None, state=None, project="", context=None,
                  idempotency_key=""):
    """Return response text if `content` is a recognized slash command, else None."""
    state = _state_or_legacy(state)

    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(None, 1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""
    _serve_logger.debug(f"_handle_slash: cmd={cmd!r}")

    # One choke point in front of every branch below, for the same reason the
    # REPL has one: this is a flat chain of ~130 `if cmd == ...` returns, and a
    # check placed after even one of them leaves that one ungated.
    refusal = _http_slash_refusal(cmd, arg, context=context)
    if refusal:
        _serve_logger.debug(f"_handle_slash: refused cmd={cmd!r}: {refusal!r}")
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
        # Starting a persistent run is a side effect.  A caller may not know
        # whether its connection died before or after the controller accepted
        # it, so an opt-in replay key returns the first result rather than
        # creating a sibling run.
        return _idempotent_http_action(
            context, idempotency_key, "autopilot\0%s\0%s" % (project, stripped),
            lambda: server.control_command(
                stripped,
                project=project,
                autopilot_request_owner=_task_account_scope(context),
            ),
        )
    if cmd in ("/runtime", "/models"):
        return server.control_command(stripped, project=project)
    if cmd in ("/update", "/updatecheck", "/updatesource"):
        return server.control_command(stripped, project=project)
    if cmd in ("/stash", "/runtime-stash"):
        return server.control_command(stripped, project=project)
    if cmd in ("/selfmod", "/selfmodify"):
        return server.control_command(stripped, project=project)
    if cmd in ("/goal", "/goals"):
        return _idempotent_http_action(
            context, idempotency_key, "goal\0%s\0%s" % (project, stripped),
            lambda: server.control_command(stripped, project=project),
        )
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
        return _idempotent_http_action(
            context, idempotency_key, "workbench\0%s\0%s" % (project, arg.strip()),
            lambda: server.workbench_agent(
                prompt=arg.strip(), tier="auto", max_steps=12, project=project,
            ),
        )
    if cmd in (
        "/report", "/endreport", "/checklist", "/plan",
        "/inventory", "/workspace",
        "/tree", "/folders", "/search", "/grep",
        "/programs", "/programfind", "/scripts", "/scriptfind",
        "/image", "/inspectimage", "/vision", "/analyzeimage",
        "/mkdir", "/runprogram", "/runscript",
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
        from ...domain.cloud_access import has_legacy_error_prefix
        if marker in out and not has_legacy_error_prefix(out):
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
    with server.approved_call_reach(tool_name, kwargs):
        return _run_catalogued_tool_gated(
            line, tool_name, kwargs, handler, state=state, context=context,
        )


def _run_catalogued_tool_gated(line, tool_name, kwargs, handler, *, state, context):
    """The gated ``/<tool>`` dispatch, run inside the call's reach scope."""
    refusal = _http_tool_refusal(
        (tool_name,), "/" + tool_name, context=context, arguments=kwargs,
    )
    if refusal:
        return refusal
    if tool_name == "loop":
        refusal = _loop_global_operation_refusal(kwargs.get("actions_json"), context)
        if refusal:
            return refusal
    task_boundary_error = _account_task_boundary_refusal(tool_name, kwargs, context)
    if task_boundary_error:
        return task_boundary_error
    # ``server.tool_manifest`` is intentionally the complete direct-MCP
    # owner catalog. Do not relay it over an account-backed HTTP request: the
    # same role filter used by /v1/commands covers both discovery spellings,
    # including the capability fingerprint endpoint.
    if context is not None and context.get("mode") != "local-open":
        if tool_name == "tool_manifest":
            return _served_tool_manifest(context)
        if tool_name == "tool_capability_manifest":
            return _served_tool_capability_manifest(context)
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
        _serve_logger.error(f"catalogued tool execution failed: tool={tool_name!r}", exc_info=True)
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
    if signal and server.reward_rules.reward_score(signal) > 0:
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


def _work_project_for_request(project, storage_project):
    """The project routed natural work runs under for one served request.

    Durable state stays namespaced per principal (``storage_project``); the
    workbench agent needs a directory. A client value that names an existing
    directory inside the configured file roots is passed through as that
    directory, which scopes the agent without widening its reach (measured
    2026-09-03: an agent with no scope resolved the client's relative paths
    against the package directory). Anything else keeps the namespaced id.
    """
    return server.served_work_project(project) or storage_project


def _handle_work_intent(content, project="", authorized=False, context=None,
                        idempotency_key=""):
    """Route developer work through the bounded execution-mode chooser."""
    refusal = intents.containment_egress_refusal(content)
    if refusal:
        return refusal
    if not authorized:
        return None
    # Do not let ordinary keyed chat occupy the bounded replay cache.  The
    # work router itself returns None for those turns, but caching that miss
    # would evict a completed mutating action which a client may still retry.
    # This is a pure host-side preflight; route_work_request repeats the same
    # classification under its stable execution context before any work starts.
    if not (
        server.master_orchestrator.requested_worker_cap(content)
        or intents.classify_execution(content)
    ):
        return None
    return _idempotent_http_action(
        context, idempotency_key, "natural-work\0%s\0%s" % (project, content),
        lambda: server.route_work_request(content, project=project),
    )


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


def _uses_default_model_route(model):
    """Whether a request leaves model selection to Sonder's default router.

    OpenAI-style ``gpt-*`` names deliberately retain the compatibility default
    unless the live catalog later resolves one as a concrete local model.  This
    helper is intentionally syntactic: it runs before rate limiting and must
    not trigger catalog discovery or other I/O.
    """
    m = str(model or "").strip()
    return not m or m in ("sonder", "local") or m.startswith("gpt-")


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

    try:
        records = server.discovered_model_records()
    except Exception:
        records = ()

    # ``sonder`` is a runtime route ID, not weights.  Tier IDs are retained for
    # compatibility, then exact live catalog names make valid installed models
    # discoverable to standard OpenAI clients.  Do not nevertheless advertise
    # a local tier that a legacy/manual policy points at an explicitly
    # non-chat-capable catalog record: `/runtime set` now rejects that state,
    # while this keeps pre-existing policy files honest too.  Missing or
    # capability-less metadata remains listed rather than converting a catalog
    # outage into a false claim that the whole runtime has no model routes.
    add("sonder", "local")
    for tier_name, model in server.available_tiers().items():
        cloud = server._is_cloud_tier(tier_name, model)
        if not cloud and server._runtime_model_capability_error(
            tier_name, model, records,
        ):
            continue
        add(tier_name, "cloud" if cloud else "local")
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
    state=None, return_result=False, metrics=None, augment=True, cache_scope="",
):
    """Call Sonder Runtime's learning loop with the UI's prior turns; returns UI text."""
    _serve_logger.debug(f"_run_prompt: tier={tier!r}, session={session!r}, project={project!r}, augment={augment}")
    state = _state_or_legacy(state)
    resolved_target = {}
    cache_state = {}

    def record_cache(status):
        # Bounded, content-free consultation result.  The closed set is
        # enforced here as well as at the metric so an unexpected value from
        # the generation layer can neither reach the receipt nor create a
        # new metric label.
        if status in ("hit", "miss"):
            cache_state["status"] = status
            if metrics is not None:
                metrics.observe_request_cache(status)

    def record_target(model, tier_label, _cloud):
        # This callback is invoked by the generation path after it has accepted
        # the route.  Model/tier names are catalog/config values, but still
        # constrain them before presenting a receipt to an HTTP caller.
        resolved_target["model"] = _receipt_text(model)
        resolved_target["tier"] = _receipt_text(tier_label)
        resolved_target["cloud"] = bool(_cloud)

    started = time.monotonic()
    outcome = "error"
    try:
        out = server.answer_with_history(
            prompt, history, trace=state.trace, strict=state.strict, tier=tier,
            context_size=context_size, session=session, project=project,
            raise_model_errors=True, target_observer=record_target, augment=augment,
            cache_scope=cache_scope, cache_observer=record_cache,
            # This surface captures the turn itself (``_capture_live_session_turn``)
            # under its own correlation id and fails the request closed when
            # it cannot; the legacy path must not capture the same turn again.
            capture_session=False,
        )
        outcome = "error" if out.startswith("ERROR") else "ok"
    finally:
        # This is the primary generation selected for an HTTP turn. Keep the
        # exported labels independent of exact models and configured aliases.
        # A control route has no target and must not masquerade as a model call.
        # A replay has no model invocation.  It still reports its bounded cache
        # result separately, but must not inflate inference counters/latency.
        if (metrics is not None and "cloud" in resolved_target
                and cache_state.get("status") != "hit"):
            metrics.observe_model_call(
                cloud=resolved_target["cloud"], result=outcome,
                elapsed_seconds=time.monotonic() - started,
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
        cache_state.get("status", ""),
    )
    return result if return_result else result.content


def _run_structured_prompt(
    prompt, history, tier, schema, context_size="", metrics=None,
):
    """Run the isolated structured path; never add a footer or activity text."""
    resolved_target = {}

    def record_target(model, tier_label, _cloud):
        resolved_target["model"] = _receipt_text(model)
        resolved_target["tier"] = _receipt_text(tier_label)
        resolved_target["cloud"] = bool(_cloud)

    started = time.monotonic()
    outcome = "error"
    try:
        content = server.structured_answer_with_history(
            prompt, history, schema, tier=tier, context_size=context_size,
            target_observer=record_target,
        )
        outcome = "ok"
    finally:
        if metrics is not None and "cloud" in resolved_target:
            metrics.observe_model_call(
                cloud=resolved_target["cloud"], result=outcome,
                elapsed_seconds=time.monotonic() - started,
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
# Public adapter compatibility value used by schema callers before bootstrap
# injection. Runtime validation still reads the injected verifier bound below.
_STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS = 256
# ``json_schema_verifier`` intentionally compares arbitrary JSON values for
# ``uniqueItems`` rather than relying on hashing. That is the correct JSON
# equality semantics, but is quadratic in the array length. HTTP structured
# output therefore needs an explicit finite host bound before asking a model to
# produce such an array.
# Keep HTTP admission and server-side verification on the same host bound. A
# model can still return more elements than its decoder schema requested.
def _structured_unique_items_max_items():
    """Read the shared verifier bound after the runtime has been injected."""
    return server._STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS


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
                % _structured_unique_items_max_items(),
            )
        if maximum > _structured_unique_items_max_items():
            raise _response_format_error(
                "uniqueItems=true requires maxItems no greater than %d"
                % _structured_unique_items_max_items(),
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
    receipt=None, activity_response=None,
):
    iid = iid or uuid.uuid4().hex[:12]
    activity = server.activity_tracker.public_response(
        activity_response, include_detail=False,
    ) if isinstance(activity_response, dict) else (
        server.activity_tracker.public_snapshot(include_detail=False) or {}
    ).get("latest")
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
        "usage": chat_formatting.chat_usage(activity_response),
        "sonder_activity": activity,
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


def _chunk(iid, model, delta, finish_reason=None, elapsed_ms=None, receipt=None,
           usage=None, activity=None):
    obj = {
        "id": "chatcmpl-%s" % iid,
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": (
            [] if usage is not None and not delta and finish_reason is None else
            [{"index": 0, "delta": delta, "finish_reason": finish_reason}]
        ),
    }
    if elapsed_ms is not None:
        obj["sonder_elapsed_ms"] = max(0, int(elapsed_ms))
    if receipt:
        obj["sonder_receipt"] = receipt
    if usage is not None:
        obj["usage"] = usage
    if activity is not None:
        # The streaming counterpart of the bounded vendor extension returned
        # by _chat_completion_object.  Keep execution evidence out of content
        # so callers can safely replay assistant text as conversation history.
        obj["sonder_activity"] = activity
    return "data: %s\n\n" % json.dumps(obj)


def _served_command_visible(command, context=None):
    """Whether a command's schema belongs in this served caller's catalog.

    The command catalog is more than cosmetic: it contains every argument
    name, default, and a purpose-built usage string.  Returning that catalog
    unchanged to an ordinary account made the HTTP authorization gate
    decorative for discovery -- an account that could not invoke
    ``/runtime_policy_update`` still received its full mutation schema from
    ``/v1/commands`` (and again via completion, help, and ``/tool_manifest``).

    Direct MCP and local-open remain deliberate owner surfaces, so their
    catalogs stay complete.  In an account-backed deployment, hide commands
    that this context could not invoke because they require developer/admin
    authority.  Permission modes are intentionally *not* filtered here: they
    are operator-selected, live state and do not define a caller's standing
    authority.
    """
    if context is None or context.get("mode") == "local-open":
        return True

    tool = str(getattr(command, "tool", "") or "")
    if tool:
        operation = _http_system_operation_for(tool)
        if operation == tool_contract.SYSTEM_OPERATION_UNBOUND:
            return _admin_authorized(context)
        if operation and _system_operation_authority_error(operation, context):
            return False

    # Native branches do not have parameter schemas, but some of them still
    # create host work or expose privileged operational state.  Keep ordinary
    # accounts from using the command catalog as an oracle for that surface.
    if bool(getattr(command, "native", False)):
        name = str(getattr(command, "name", "") or "")
        if _dangerous_http_slash(name) and not _developer_authorized(context):
            return False
    return True


def _served_commands(context=None):
    """Return catalog rows visible to one HTTP authorization context."""
    return tuple(
        command for command in command_catalog.catalog()
        if _served_command_visible(command, context)
    )


def _served_http_commands(context=None):
    """Return the caller-visible catalog commands the HTTP dispatcher handles."""
    http_names = {command.name for command in command_catalog.http_catalog()}
    return tuple(
        command for command in _served_commands(context)
        if command.name in http_names
    )


def _served_help_text(topic, context=None):
    """Render help from the caller-filtered catalog without leaking misses."""
    commands = _served_commands(context)
    needle = str(topic or "").strip().lower()
    if not needle:
        categories = sorted({command.category for command in commands})
        return (
            "Sonder commands available to this account (use /v1/commands for details):\n"
            + "  " + ", ".join(categories)
        )
    if needle in command_catalog.CATEGORIES:
        rows = [command for command in commands if command.category == needle]
        if not rows:
            return "No commands in that category are available to this account."
        return "%s --\n%s" % (
            needle,
            "\n".join("  %s  %s" % (row.name, row.summary) for row in rows),
        )
    for command in commands:
        if needle == command.name or needle in command.aliases:
            return "%s\nusage: %s\n%s" % (
                command.name, command.usage(), command.summary,
            )
    # Do not distinguish an unknown command from one intentionally hidden.
    return "No command with that name is available to this account."


def _served_tool_manifest(context):
    """Human-readable HTTP manifest that excludes unreachable tool schemas."""
    rows = _served_commands(context)
    return "\n".join(
        "  %s: %s" % (command.name, command.summary) for command in rows
    )


def _served_tool_capability_manifest(context):
    """Fingerprint the served, caller-visible command schema surface only."""
    rows = [command.to_dict() for command in _served_commands(context)]
    canonical = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )
    return json.dumps({
        "sha256": hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        "tool_count": len(rows),
        "manifest": _served_tool_manifest(context),
        "authority": "informational only; host policy remains authoritative",
    }, indent=2, sort_keys=True)


def _commands_index_payload(context=None):
    # A catalog that cannot read the tool registry now raises rather than
    # quietly returning nothing (command_catalog.CatalogUnavailable). This is
    # a listing endpoint, not an enforcing one, so it degrades -- but it says
    # so in the payload instead of shipping an empty list the client would
    # render as "this build has no commands".
    try:
        commands = [command.to_dict() for command in _served_http_commands(context)]
        error = ""
    except command_catalog.CatalogUnavailable as exc:
        commands, error = [], str(exc)
    payload = {
        "commands": commands,
        # The blurb per category, not the commands in it: the client renders
        # these as section headings beside the counts it derives itself.
        "categories": dict(command_catalog.CATEGORIES),
        "popular": [
            name for name in command_catalog.POPULAR
            if any(command.name == name for command in _served_commands(context))
        ],
    }
    if error:
        payload["error"] = error
    return payload


def _commands_complete_payload(query, limit="", context=None):
    try:
        # Search the complete catalog first so filtering cannot make a valid
        # lower-ranked HTTP command disappear behind console-only matches.
        visible = {command.name for command in _served_http_commands(context)}
        matches = [
            command.to_dict()
            for command in command_catalog.complete(
                query, limit=COMPLETE_MAX_LIMIT,
            )
            if command.name in visible
        ]
    except command_catalog.CatalogUnavailable as exc:
        return {"matches": [], "error": str(exc)}
    return {"matches": matches[:_completion_limit(limit)]}


def _commands_help_payload(topic="", context=None):
    if context is None or context.get("mode") == "local-open":
        requested = str(topic or "").strip()
        catalogued = command_catalog.by_name(requested) if requested else None
        if catalogued is not None and catalogued.native:
            wanted = requested.lower()
            if not wanted.startswith("/"):
                wanted = "/" + wanted
            http_spellings = {
                spelling.lower()
                for command in command_catalog.http_catalog()
                for spelling in command.all_names
            }
            if wanted not in http_spellings:
                return {"text": "no HTTP command '%s'." % requested}
        return {"text": command_catalog.help_text(topic)}
    try:
        return {"text": _served_help_text(topic, context)}
    except command_catalog.CatalogUnavailable as exc:
        return {"text": "Command catalog unavailable: %s" % exc}


class Handler(BaseHTTPRequestHandler):
    server_version = "sonder-serve/1.0"
    # socketserver reads this in setup() and calls connection.settimeout(); a
    # timed-out read raises in handle_one_request, which closes the connection.
    timeout = REQUEST_TIMEOUT_SECONDS

    def finish(self):
        """Soak an abandoned request body so the peer still reads the response."""
        try:
            self._linger_before_close()
        finally:
            super().finish()

    def _linger_before_close(self):
        """Bounded lingering close for a connection ending on an unread body.

        Only reached when a response deliberately skipped the body and ended
        the connection (see ``_settle_unread_request_body``). Nothing read here
        is parsed or retained; the point is purely that the peer's in-flight
        bytes do not turn the close into a reset that erases the error the
        caller needs to see.
        """
        if not self.close_connection:
            return
        if getattr(self, "_request_body_consumed", True):
            return
        connection = getattr(self, "connection", None)
        wfile = getattr(self, "wfile", None)
        if connection is None or wfile is None:
            return
        deadline = time.monotonic() + LINGERING_CLOSE_SECONDS
        remaining = LINGERING_CLOSE_BYTES
        try:
            wfile.flush()
            connection.settimeout(LINGERING_CLOSE_SECONDS)
            while remaining > 0 and time.monotonic() < deadline:
                chunk = connection.recv(min(remaining, 65536))
                if not chunk:
                    break
                remaining -= len(chunk)
        except (OSError, ValueError):
            return

    def handle(self):
        """Treat a client reset during HTTP framing as a normal disconnect."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # This can happen before a request reaches do_GET/do_POST, so the
            # response helpers cannot catch it.  It is a peer disconnect, not
            # a server exception worth emitting as a socketserver traceback.
            self.close_connection = True

    def _cors(self):
        origin = self.headers.get("Origin")
        if origin is not None and origin in CORS_ORIGINS:
            _serve_logger.debug(f"_cors: allowing origin={origin!r}")
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header(
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Sonder-Account-Token, "
                "X-Sonder-Bootstrap-Secret, Idempotency-Key",
            )
            self.send_header(
                "Access-Control-Expose-Headers",
                "X-Sonder-Elapsed-Ms, X-Sonder-Correlation-Id",
            )

    def log_message(self, fmt, *args):
        sys.stderr.write("[sonder_serve] %s\n" % (fmt % args))

    def do_OPTIONS(self):
        # BaseHTTPRequestHandler may reuse this Handler for several HTTP/1.1
        # requests. A correlation ID is a request receipt, never a socket
        # receipt, so discard the prior request's cached value first.
        self._correlation_id = ""
        self._operation_context = None
        self._request_started = time.monotonic()
        self._request_body_consumed = False
        if self._reject_disallowed_origin():
            return
        must_close = self._close_for_unread_body()
        self.send_response(204)
        self._cors()
        if must_close:
            self.send_header("Connection", "close")
        self.send_header("X-Sonder-Elapsed-Ms", "0")
        self.end_headers()

    def _reject_disallowed_origin(self):
        origin = self.headers.get("Origin")
        if origin is None or origin in CORS_ORIGINS:
            return False
        _serve_logger.debug(f"_reject_disallowed_origin: rejecting origin={origin!r}")
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

    def _client_ip(self):
        """Resolve client IP through X-Forwarded-For when peer is a trusted proxy."""
        peer = self._peer()
        if not peer:
            return peer
        try:
            peer_addr = ipaddress.ip_address(peer)
        except ValueError:
            return peer
        if not any(peer_addr in net for net in _TRUSTED_PROXY_NETWORKS):
            return peer
        xff = self.headers.get("X-Forwarded-For", "")
        if not xff:
            return peer
        parts = [p.strip() for p in xff.split(",") if p.strip()]
        if not parts:
            return peer
        return parts[0]

    def _correlation(self):
        if not getattr(self, "_correlation_id", ""):
            self._correlation_id = sonder_lifecycle.new_correlation_id()
        return self._correlation_id

    def _send_auth_error(self, reason="invalid-credentials"):
        _serve_logger.debug(f"_send_auth_error: peer={self._peer()!r}, reason={reason!r}")
        sonder_lifecycle.get().record_auth_failure(self._client_ip(), reason)
        self._send_json_payload({
            "error": {"message": "authentication required", "type": "auth",
                      "code": "UNAUTHENTICATED",
                      "correlation_id": self._correlation()},
        }, status=401)

    def _auth_rate_limited(self):
        """Token-bucket authentication-failure limiter (admission step 3)."""
        client = self._client_ip()
        if sonder_lifecycle.get().auth_attempt_allowed(client):
            return False
        _serve_logger.error(f"auth rate limit exceeded for client={client!r}")
        _serve_logger.debug(f"_auth_rate_limited: rate-limited client={client!r}")
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
        route = _HEALTH_STATUS_FACADE.route(path)
        if route is None:
            return False
        _serve_logger.debug(f"_handle_lifecycle_get: path={path!r}, requires_auth={route.requires_auth}")
        if route.requires_auth and not self._peer_is_loopback():
            if self._auth_rate_limited():
                return True
            if not self._request_auth_context()["authorized"]:
                _serve_logger.debug(f"_handle_lifecycle_get: auth failed for path={path!r}")
                self._send_auth_error()
                return True
        status, payload = route.render(lifecycle)
        if route.media_type == "application/json":
            self._send_json_payload(payload, status=status)
            return True
        body = payload
        must_close = self._close_for_unread_body()
        self.send_response(200)
        self._cors()
        if must_close:
            self.send_header("Connection", "close")
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
            _serve_logger.info("Admin drain requested, initiating graceful shutdown")
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
            _request_idempotency_key(
                context, "/v1/admin/drain", self.headers.get("Idempotency-Key", ""),
            ),
            start_drain,
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
            or not token_is_configured(LAUNCHER_HEALTH_TOKEN)
            or RUNTIME_ROLE != sonder_health.MANAGED_ROLE
            or not sonder_health.nonce_is_valid(nonce)
        ):
            return ""
        return nonce

    def _unread_request_body_bytes(self):
        """Declared body bytes this handler has not taken off the socket.

        ``None`` means the body boundary cannot be located at all -- a transfer
        coding, or a missing/duplicated/non-numeric ``Content-Length`` -- so
        nothing may be assumed about where the next request begins.
        """
        if getattr(self, "_request_body_consumed", False):
            return 0
        if self.headers.get_all("Transfer-Encoding"):
            return None
        lengths = self.headers.get_all("Content-Length") or ()
        if not lengths:
            return 0
        if len(lengths) > 1:
            return None
        raw = lengths[0].strip()
        if not raw.isdigit():
            return None
        try:
            return int(raw)
        except ValueError:
            # Unicode decimal digits and very long digit strings can pass
            # isdigit() yet still be rejected by int(). Treat either as
            # unframed so the caller closes rather than dropping its response.
            return None

    def _settle_unread_request_body(self):
        """Return whether this response must also end the connection.

        Answering before ``_read_json`` leaves the request body queued on the
        socket. Under the HTTP/1.1 deployment option the handler stays on that
        connection, so those bytes would be read as the next request line: the
        caller's following request silently disappears and a forged one takes
        its place. Discard a small, fully framed body so ordinary reuse still
        works; an oversized or unframed body is never read, and its connection
        is closed instead. See MAX_DISCARDED_BODY_BYTES.
        """
        pending = self._unread_request_body_bytes()
        if pending == 0:
            self._request_body_consumed = True
            return False
        if pending is None or pending > MAX_DISCARDED_BODY_BYTES:
            return True
        connection = getattr(self, "connection", None)
        prior = None
        restore = False
        try:
            if connection is not None:
                prior = connection.gettimeout()
                if prior is None or prior > DISCARD_BODY_TIMEOUT_SECONDS:
                    connection.settimeout(DISCARD_BODY_TIMEOUT_SECONDS)
                    restore = True
            discarded = self.rfile.read(pending)
        except (OSError, ValueError):
            return True
        finally:
            if restore:
                try:
                    connection.settimeout(prior)
                except OSError:
                    pass
        if len(discarded) != pending:
            return True
        self._request_body_consumed = True
        return False

    def _close_for_unread_body(self):
        """Settle the request body and latch the close before writing headers."""
        must_close = self._settle_unread_request_body()
        if must_close:
            # Latch it here as well: a delivery failure below must not leave a
            # desynchronized connection open just because the header write lost
            # its race with the peer.
            self.close_connection = True
        return must_close

    def _send_json_payload(self, payload, status=200, headers=None, elapsed_ms=None):
        _serve_logger.debug(f"_send_json_payload: status={status}")
        body = json.dumps(payload).encode("utf-8")
        # Keep this low-level delivery helper usable by the focused socket
        # probes that intentionally provide only the response-writer surface.
        # Real Handler instances always expose the settlement hook.
        settle_unread_body = getattr(self, "_close_for_unread_body", None)
        must_close = settle_unread_body() if callable(settle_unread_body) else False
        try:
            self.send_response(status)
            self._cors()
            if must_close:
                self.send_header("Connection", "close")
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

    def _send_binary_payload(self, body, *, content_type, digest, status=200):
        if not isinstance(body, bytes):
            raise TypeError("binary response body must be bytes")
        must_close = self._close_for_unread_body()
        try:
            self.send_response(status)
            self._cors()
            if must_close:
                self.send_header("Connection", "close")
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-Sonder-Artifact-Sha256", digest)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
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
        try:
            operation_context = getattr(self, "_operation_context", None)
            if operation_context is None:
                operation_context = lifecycle.operation_context(
                    self._correlation(), None,
                )
            lifecycle.trace_operation(
                operation_context,
                operation="http.chat_completion",
                status="ok" if result == "ok" else "error",
                duration_ms=max(0.0, (time.monotonic() - started) * 1000.0),
                labels={"result": result},
            )
        except Exception:
            # Telemetry remains bounded and non-authoritative; request handling
            # must not become dependent on its optional inspection surface.
            pass

    def _read_json(self):
        # HTTP framing must be unambiguous before this handler reads a body.
        self._validate_request_framing()
        content_lengths = self.headers.get_all("Content-Length") or ()
        raw_length = content_lengths[0] if content_lengths else None
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
            # The peer stopped mid-body, so the declared span is gone for good.
            # Leave the request marked unconsumed: the response path then ends
            # this connection rather than resuming inside a truncated body.
            raise HTTPRequestError(400, "request body is incomplete")
        # The declared body is off the socket, whatever it decodes to below.
        self._request_body_consumed = True
        try:
            payload = json.loads(
                raw.decode("utf-8"),
                object_pairs_hook=_reject_duplicate_json_object_key,
                parse_constant=_reject_nonfinite_json_number,
            )
        except _DuplicateJsonObjectKey:
            raise HTTPRequestError(400, "request JSON must not contain duplicate object keys")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
            raise HTTPRequestError(400, "request body must contain valid JSON")
        if not isinstance(payload, dict):
            raise HTTPRequestError(400, "request JSON must be an object")
        return payload

    def _validate_request_framing(self):
        """Reject ambiguous body framing before any POST route dispatches."""
        # BaseHTTPRequestHandler preserves duplicate fields, but ``get()``
        # returns only one of them. Accepting that value would let a proxy and
        # this server disagree about where the request ends. We deliberately
        # support neither transfer coding nor duplicate content lengths, so
        # reject the fields by presence/count rather than their first value.
        transfer_encodings = self.headers.get_all("Transfer-Encoding") or ()
        if transfer_encodings:
            raise HTTPRequestError(400, "transfer encoding is not supported")
        content_lengths = self.headers.get_all("Content-Length") or ()
        if len(content_lengths) > 1:
            raise HTTPRequestError(400, "multiple Content-Length headers are not supported")

    def do_GET(self):
        # Keep-alive reuses Handler instances; see do_OPTIONS for why this is
        # reset before every externally visible request.
        self._correlation_id = ""
        self._request_started = time.monotonic()
        self._request_body_consumed = False
        if self._reject_disallowed_origin():
            return
        path = _request_route(self.path)
        _serve_logger.debug(f"do_GET: path={path!r}, peer={self._peer()!r}")
        if path == "/" and _local_log_dashboard_allowed(self._peer()):
            self._send_local_log_page()
            return
        if path == "/v1/local/server-log":
            if not _local_log_dashboard_allowed(self._peer()):
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
        if path == "/v1/compute/snapshot":
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            from sonder_runtime.bootstrap.app import default_app
            from sonder_runtime.interfaces.http.facades.compute_fabric import (
                dispatch_compute_snapshot,
            )

            snapshot_factory = default_app().compute_snapshot
            if snapshot_factory is None:
                _serve_logger.warning("compute snapshot requested but factory is unavailable")
                self._send_json_payload(
                    {"error": {"message": "compute snapshot is unavailable",
                               "type": "server_error",
                               "code": "COMPUTE_SNAPSHOT_UNAVAILABLE"}},
                    status=503,
                )
                return
            try:
                result = dispatch_compute_snapshot(snapshot_factory)
            except Exception as exc:
                _serve_logger.error(f"compute snapshot failed, correlation={self._correlation()!r}", exc_info=True)
                self.log_error("compute snapshot failed: %s", type(exc).__name__)
                self._send_json_payload(
                    {"error": {"message": "compute snapshot is unavailable",
                               "type": "server_error",
                               "code": "COMPUTE_SNAPSHOT_UNAVAILABLE"}},
                    status=503,
                )
                return
            self._send_json_payload(result.body, status=result.status_code)
            return
        compute_route = _compute_job_route(path)
        if compute_route is not None and compute_route[0] in (
            "status", "by_idempotency", "artifact",
        ):
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            if not _admin_authorized(context):
                self._send_json_payload(
                    {"error": {"message": "administrator authorization is required",
                               "type": "forbidden", "code": "FORBIDDEN"}},
                    status=403,
                )
                return
            from sonder_runtime.bootstrap.app import default_app
            from sonder_runtime.interfaces.http.facades.compute_fabric import (
                dispatch_compute_job_by_idempotency,
                dispatch_compute_job_artifact,
                dispatch_compute_job_status,
            )

            worker_factory = default_app().compute_job_worker
            if worker_factory is None:
                _serve_logger.warning("compute job worker requested but factory is unavailable")
                self._send_json_payload(
                    {"error": {"message": "compute job worker is unavailable",
                               "type": "server_error",
                               "code": "COMPUTE_JOB_WORKER_UNAVAILABLE"}},
                    status=503,
                )
                return
            operation, identity = compute_route
            try:
                if operation == "status":
                    result = dispatch_compute_job_status(worker_factory(), identity)
                elif operation == "by_idempotency":
                    result = dispatch_compute_job_by_idempotency(worker_factory(), identity)
                else:
                    remote_job_id, artifact_name = identity
                    artifact_result = dispatch_compute_job_artifact(
                        worker_factory(), remote_job_id, artifact_name,
                    )
                    artifact = artifact_result.payload
                    self._send_binary_payload(
                        artifact.content,
                        content_type=artifact.receipt.mime_type,
                        digest=artifact.receipt.sha256,
                        status=artifact_result.status_code,
                    )
                    return
            except (NotFound, KeyError):
                self._send_not_found()
                return
            except (InvalidInput, ValueError, TypeError) as error:
                self._send_json_payload(
                    {"error": {"message": str(error), "type": "invalid_request"}},
                    status=400,
                )
                return
            except Exception as exc:
                _serve_logger.error(f"compute job status failed: operation={operation!r}, correlation={self._correlation()!r}", exc_info=True)
                self.log_error("compute job status failed: %s", type(exc).__name__)
                self._send_json_payload(
                    {"error": {"message": "compute job status is unavailable",
                               "type": "server_error",
                               "code": "COMPUTE_JOB_UNAVAILABLE"}},
                    status=503,
                )
                return
            if result is None:
                self._send_not_found()
                return
            self._send_json_payload(result.body, status=result.status_code)
            return
        if _is_extension_route(path):
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            if not _admin_authorized(context):
                self._send_json_payload(
                    {"error": {"message": "administrator authorization is required",
                                "type": "forbidden", "code": "FORBIDDEN"}},
                    status=403,
                )
                return
            from sonder_runtime.bootstrap.app import default_app

            application = default_app()
            facade_factory = application.extension_facade
            if facade_factory is None:
                _serve_logger.warning("extension route GET requested but extension facade is unavailable")
                self._send_json_payload(
                    {"error": {"message": "extension facade is unavailable",
                                "type": "server_error", "code": "EXTENSION_FACADE_UNAVAILABLE"}},
                    status=503,
                )
                return
            result = dispatch_extension_route(
                facade_factory(), "GET", path, None, _extension_authority(context)
            )
            if result is None:
                self._send_not_found()
                return
            self._send_json_payload(result.body, status=result.status_code)
            return
        if _is_trace_projection_route(path):
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            if not _admin_authorized(context):
                self._send_json_payload(
                    {"error": {"message": "administrator authorization is required",
                                "type": "forbidden", "code": "FORBIDDEN"}},
                    status=403,
                )
                return
            from sonder_runtime.bootstrap.app import default_app

            query = urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query,
                keep_blank_values=True,
            )
            result = dispatch_trace_route(default_app().events, "GET", path, query)
            if result is None:
                self._send_not_found()
                return
            self._send_json_payload(result.body, status=result.status_code)
            return
        _maybe_live_reload()
        if path.startswith("/v1/sessions/"):
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            if not _admin_authorized(context):
                self._send_json_payload(
                    {"error": {"message": "administrator authorization is required",
                                "type": "forbidden", "code": "FORBIDDEN"}},
                    status=403,
                )
                return
            facade = _SESSION_FACADE
            if facade is None:
                _serve_logger.warning("session route requested but session facade is not configured")
                self._send_json_payload(
                    {"error": {"message": "session facade is unavailable",
                                "type": "server_error", "code": "SESSION_FACADE_UNAVAILABLE"}},
                    status=503,
                )
                return
            result = dispatch_session_route(
                facade, path,
                query=urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query,
                    keep_blank_values=True,
                ),
            )
            if result is None:
                self._send_not_found()
                return
            self._send_json_payload(result.body, status=result.status_code)
            return
        if path == "/v1/jobs" or path.startswith("/v1/jobs/"):
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
            from sonder_runtime.bootstrap.app import default_app

            application = default_app()
            stream_job_id = _job_subroute_id(path, "/stream")
            result_job_id = _job_subroute_id(path, "/result")
            if stream_job_id is not None:
                try:
                    after, max_events, max_bytes = _job_stream_query(
                        urllib.parse.urlsplit(self.path).query
                    )
                except ValueError as error:
                    self._send_json_payload(
                        {"error": {"message": str(error), "type": "invalid_request"}},
                        status=400,
                    )
                    return
                try:
                    page = application.job_registry().stream(
                        stream_job_id,
                        after=OutputWatermark(after),
                        max_events=max_events,
                        max_bytes=max_bytes,
                    )
                except KeyError:
                    self._send_not_found()
                    return
                self._send_json_payload({
                    "object": "job_output",
                    "job_id": stream_job_id,
                    "events": [_job_output_event_payload(event) for event in page.events],
                    "next_watermark": page.next_watermark.sequence,
                    "has_more": page.has_more,
                    "truncated": page.truncated,
                })
                return
            if result_job_id is not None:
                try:
                    record = application.job_registry().collect(result_job_id)
                except KeyError:
                    self._send_not_found()
                    return
                except ValueError as error:
                    self._send_json_payload(
                        {"error": {"message": str(error), "type": "conflict"}},
                        status=409,
                    )
                    return
                self._send_json_payload({
                    "object": "job_result",
                    "job_id": result_job_id,
                    "status": record.status.value,
                    "result": record.result,
                    "error": record.error,
                    "job": _job_record_payload(record),
                })
                return

            service = application.job_service()
            if path == "/v1/jobs":
                query = urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query,
                    keep_blank_values=True,
                )
                raw_limit = query.get("limit", ["100"])[-1]
                try:
                    limit = int(raw_limit)
                except (TypeError, ValueError):
                    self._send_json_payload(
                        {"error": {"message": "limit must be an integer between 1 and 100", "type": "invalid_request"}},
                        status=400,
                    )
                    return
                if limit < 1 or limit > 100:
                    self._send_json_payload(
                        {"error": {"message": "limit must be an integer between 1 and 100", "type": "invalid_request"}},
                        status=400,
                    )
                    return
                records = service.list(limit=limit)
                self._send_json_payload({
                    "object": "list",
                    "data": [_job_record_payload(record) for record in records],
                })
                return
            job_id = _job_record_id(path)
            if job_id is None:
                self._send_not_found()
                return
            try:
                record = service.get(job_id)
            except NotFound:
                self._send_not_found()
                return
            self._send_json_payload(_job_record_payload(record))
            return
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
                import sonder_runtime.adapters.updates.engine as sonder_update_engine

                payload = sonder_update_engine.UpdateManager().status()
            except Exception as error:
                _serve_logger.error(f"update status failed, correlation={self._correlation()!r}", exc_info=True)
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
        if _CONTROL_PLANE_FACADE.route(path) is not None:
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
            route = _CONTROL_PLANE_FACADE.route(path)
            if _CONTROL_PLANE_SERVICE is None:
                self._send_json_payload(
                    {"error": "control_plane_unavailable"}, status=503
                )
                return
            status, payload = route.render(
                _CONTROL_PLANE_SERVICE,
                captured_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            )
            self._send_json_payload(payload, status=status)
            return
        if _A2A_AGENT_CARD_FACADE.route(path) is not None:
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
            base_url = _a2a_discovery_base_url()
            if not base_url:
                self._send_json_payload(
                    {"error": "a2a_discovery_unavailable"}, status=503
                )
                return
            from sonder_runtime.bootstrap.app import default_app

            application = default_app()
            registry_factory = application.agent_registry
            if registry_factory is None:
                self._send_json_payload(
                    {"error": "a2a_discovery_unavailable"}, status=503
                )
                return
            card = _A2A_AGENT_CARD_FACADE.card(
                registry_factory().registrations,
                base_url=base_url,
            )
            route = _A2A_AGENT_CARD_FACADE.route(path)
            status, payload = route.render(card)
            self._send_json_payload(payload, status=status)
            return
        if path == "/v1/sonder/status":
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            account = context["account"]
            # This endpoint is a host-wide operations dashboard, not a
            # per-account session view.  Its agent, activity, learning and
            # durable-store sections have no principal boundary to apply at
            # this adapter.  Returning a redacted *global* snapshot would
            # still disclose another account's workload and timing, so keep
            # it available to the local owner / administrator only.
            if not _admin_authorized(context):
                self._send_json_payload({
                    "status": "restricted",
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
                    "operational": {
                        "available": False,
                        "reason": (
                            "host-wide diagnostics require administrator "
                            "authorization"
                        ),
                    },
                })
                return
            agents = server.master_orchestrator.snapshot()
            activity_source = server.activity_tracker.snapshot()
            detail_allowed = _execution_feed_detail_allowed(context)
            activity = server.activity_tracker.public_snapshot(
                activity_source, include_detail=detail_allowed,
            )
            from sonder_runtime.bootstrap.app import default_app
            application = default_app()
            payload = {
                "status": server.status(),
                "providers": list(application.provider_health_data()),
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
                "state_home": str(runtime_paths.default_home()),
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
        if path == "/v1/sonder/feed":
            # Owner-scoped by construction: the tracker only returns spans
            # recorded under this caller's opaque principal, so unlike
            # /v1/sonder/status no administrator gate is needed to keep one
            # account's work invisible to another.
            context = self._request_auth_context()
            if not context["authorized"]:
                self._send_auth_error()
                return
            self._send_json_payload(
                server.activity_tracker.live_feed_for_owner(
                    _feed_request_owner(context)
                )
            )
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
        must_close = self._close_for_unread_body()
        self.send_response(200)
        if must_close:
            self.send_header("Connection", "close")
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

        context = self._request_auth_context()
        if route == "/v1/commands/complete":
            payload = _commands_complete_payload(
                first("q"), first("limit"), context=context,
            )
        elif route == "/v1/commands/help":
            payload = _commands_help_payload(first("topic"), context=context)
        else:
            payload = _commands_index_payload(context=context)
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
        # Use the injected compatibility namespace rather than reaching
        # around it directly.  Besides keeping the route bound to the
        # composition-time runtime, this preserves the established patch seam
        # for callers that replace a single legacy operation in tests.
        runtime = server
        if not context["authorized"]:
            self._send_auth_error()
            return True
        _run, error = self._fanout_run_for_context(context, run_id)
        if error:
            status, message = error
            self._send_json_payload({"error": {"message": message, "type": "forbidden" if status == 403 else "not_found"}}, status=status)
            return True
        supplied_key = self.headers.get("Idempotency-Key", "")

        def replay(action_name, factory):
            # The run is already owner-authorized above.  Bind each replay to
            # both that durable run and the complete small action payload, so
            # a client key cannot turn a cancel into a resume or select a
            # different synthesis model.  _http_action_idempotency_key hashes
            # this text; neither it nor the raw header is retained.
            return _idempotent_http_action(
                context,
                supplied_key,
                "fanout\0%s\0%s" % (run_id, action_name),
                factory,
            )
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
            conn = runtime._open_db()
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
                payload = replay(
                    "synthesize\0%s" % synth_model,
                    lambda: runtime._fanout_synthesize_run(_run, synth_model),
                )
                self._send_json_payload(payload)
            except runtime.ModelCallError as exc:
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
            replay("cancel", lambda: runtime.fanout_store.request_cancel(run_id))
        else:
            for name in ("include_failed", "retry_unknown"):
                if name in req and not isinstance(req[name], bool):
                    self._send_json_payload({"error": {"message": "%s must be a boolean" % name, "type": "invalid_request"}}, status=400)
                    return True
            include_failed = req.get("include_failed") is True
            retry_unknown = req.get("retry_unknown") is True

            def resume():
                resumed = runtime.fanout_store.resume_run(
                    run_id,
                    include_failed=include_failed,
                    retry_unknown=retry_unknown,
                )
                if resumed is None:
                    return False
                # A resume is an explicit replay instruction. _execute
                # preserves the stored snapshot and never retries unknown rows
                # unless this request included retry_unknown=true.
                runtime._execute_fanout_run(run_id)
                return True

            resumed = replay(
                "resume\0include_failed=%d\0retry_unknown=%d" % (
                    include_failed, retry_unknown,
                ),
                resume,
            )
            if resumed is None:
                self._send_json_payload({"error": {"message": "fanout run is not resumable with the selected retry options", "type": "invalid_request"}}, status=400)
                return True
            if not resumed:
                self._send_json_payload({"error": {"message": "fanout run is not resumable with the selected retry options", "type": "invalid_request"}}, status=400)
                return True
        receipt = runtime._fanout_receipt(run_id)
        self._send_json_payload(receipt or {"error": {"message": "fanout receipt was unavailable", "type": "not_found"}}, status=200 if receipt else 404)
        return True

    def _handle_permission_mode_post(self, req, context=None):
        """Switch the autonomy mode. Deliberately cannot grant elevation."""
        wanted = ""
        if isinstance(req, dict):
            wanted = str(req.get("mode") or "").strip()
        if not wanted:
            self._send_json_payload(
                {"error": "mode is required", "modes": list(permission_policy.modes())},
                status=400,
            )
            return
        try:
            _idempotent_http_action(
                context,
                self.headers.get("Idempotency-Key", ""),
                "permission-mode\0%s" % wanted,
                lambda: permission_policy.set_mode(wanted),
            )
        except ValueError as exc:
            self._send_json_payload(
                {"error": str(exc), "modes": list(permission_policy.modes())},
                status=400,
            )
            return
        self._send_json_payload(server.permission_mode_data())

    def do_POST(self):
        # Keep-alive reuses Handler instances; see do_OPTIONS for why this is
        # reset before every externally visible request.
        self._correlation_id = ""
        self._request_started = time.monotonic()
        # BaseHTTPRequestHandler reuses this instance for HTTP/1.1 keep-alive
        # requests. The terminal-metric latch is per request, never per socket,
        # and so is the record of whether this request's body was read.
        self._chat_completion_metrics_recorded = False
        self._request_body_consumed = False
        is_chat_completion = _request_route(self.path) == "/v1/chat/completions"
        _serve_logger.debug(f"do_POST: path={_request_route(self.path)!r}, peer={self._peer()!r}, is_chat_completion={is_chat_completion}")
        model_operation = ""

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
        try:
            self._validate_request_framing()
        except HTTPRequestError as error:
            record_early_chat_metric("malformed_request")
            self._send_json_payload(
                {"error": {"message": error.message, "type": error.error_type}},
                status=error.status,
            )
            return
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
        lifecycle = sonder_lifecycle.get()
        operation_context = getattr(lifecycle, "operation_context", None)
        self._operation_context = (
            operation_context(self._correlation(), context)
            if callable(operation_context) else None
        )
        compute_route = _compute_job_route(path)
        if compute_route is not None and compute_route[0] in ("submit", "cancel"):
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
            from sonder_runtime.bootstrap.app import default_app
            from sonder_runtime.interfaces.http.facades.compute_fabric import (
                dispatch_compute_job_cancel,
                dispatch_compute_job_submit,
            )

            worker_factory = default_app().compute_job_worker
            if worker_factory is None:
                _serve_logger.warning("compute job submit/cancel requested but worker factory is unavailable")
                self._send_json_payload(
                    {"error": {"message": "compute job worker is unavailable",
                               "type": "server_error",
                               "code": "COMPUTE_JOB_WORKER_UNAVAILABLE"}},
                    status=503,
                )
                return
            operation, identity = compute_route
            try:
                if operation == "submit":
                    result = dispatch_compute_job_submit(worker_factory(), req)
                else:
                    reason = req.get("reason")
                    if set(req) != {"reason"} or not isinstance(reason, str):
                        raise ValueError("reason must be the only request field")
                    result = dispatch_compute_job_cancel(
                        worker_factory(), identity, reason
                    )
            except Conflict as error:
                self._send_json_payload(
                    {"error": {"message": str(error), "type": "conflict"}},
                    status=409,
                )
                return
            except NotFound:
                self._send_not_found()
                return
            except (InvalidInput, ValueError, TypeError) as error:
                self._send_json_payload(
                    {"error": {"message": str(error), "type": "invalid_request"}},
                    status=400,
                )
                return
            except Exception as exc:
                _serve_logger.error(f"compute job request failed: operation={operation!r}, correlation={self._correlation()!r}", exc_info=True)
                self.log_error("compute job request failed: %s", type(exc).__name__)
                self._send_json_payload(
                    {"error": {"message": "compute job request failed",
                               "type": "server_error",
                               "code": "COMPUTE_JOB_UNAVAILABLE"}},
                    status=503,
                )
                return
            self._send_json_payload(result.body, status=result.status_code)
            return
        if path == "/a2a":
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
            from sonder_runtime.bootstrap.app import default_app

            application = default_app()
            handler = _A2A_REQUEST_HANDLER or build_application_a2a_handler(
                application,
                base_url=os.environ.get("SONDER_A2A_BASE_URL", "").strip(),
                card_facade=_A2A_AGENT_CARD_FACADE,
            )
            result = dispatch_a2a_jsonrpc_route(handler, "POST", path, req)
            if result is None:
                self._send_not_found()
                return
            self._send_json_payload(result.body, status=result.status_code)
            return
        if _is_extension_route(path):
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
            from sonder_runtime.bootstrap.app import default_app

            application = default_app()
            facade_factory = application.extension_facade
            if facade_factory is None:
                _serve_logger.warning("extension route POST requested but extension facade is unavailable")
                self._send_json_payload(
                    {"error": {"message": "extension facade is unavailable",
                                "type": "server_error", "code": "EXTENSION_FACADE_UNAVAILABLE"}},
                    status=503,
                )
                return
            result = dispatch_extension_route(
                facade_factory(), "POST", path, req, _extension_authority(context)
            )
            if result is None:
                self._send_not_found()
                return
            self._send_json_payload(result.body, status=result.status_code)
            return
        if path == "/v1/jobs/start":
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
                from sonder_runtime.application.ports.jobs import JobIdentity
                from sonder_runtime.bootstrap.app import default_app

                identity = JobIdentity(**_job_start_payload(req))
                record = default_app().job_service().create(identity)
            except ValueError as error:
                if "already exists" in str(error):
                    self._send_json_payload(
                        {"error": {"message": "job identity already exists", "type": "conflict"}},
                        status=409,
                    )
                    return
                self._send_json_payload(
                    {"error": {"message": str(error), "type": "invalid_request"}},
                    status=400,
                )
                return
            except TypeError as error:
                self._send_json_payload(
                    {"error": {"message": str(error), "type": "invalid_request"}},
                    status=400,
                )
                return
            except Exception as error:
                # The durable adapter reports duplicate identities and parent
                # lookup failures as ValueError/KeyError.  Keep storage
                # details out of the response while preserving a conflict
                # that callers can safely retry or reconcile.
                if isinstance(error, KeyError) or "already exists" in str(error):
                    self._send_json_payload(
                        {"error": {"message": "job identity already exists", "type": "conflict"}},
                        status=409,
                    )
                    return
                _serve_logger.error(f"job start failed, correlation={self._correlation()!r}", exc_info=True)
                self.log_error("job start failed: %s", type(error).__name__)
                self._send_json_payload(
                    {"error": {"message": "job could not be started", "type": "internal_error"}},
                    status=500,
                )
                return
            self._send_json_payload(
                {"object": "job_start", "job": _job_record_payload(record)},
                status=202,
            )
            return
        job_id = _job_cancel_id(path)
        if job_id is not None:
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
            reason = req.get("reason")
            if (
                set(req) != {"reason"}
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > _MAX_JOB_CANCEL_REASON
            ):
                self._send_json_payload(
                    {"error": {
                        "message": "reason must be a non-empty string of at most 256 characters",
                        "type": "invalid_request",
                    }},
                    status=400,
                )
                return
            from sonder_runtime.bootstrap.app import default_app

            service = default_app().job_service()
            try:
                records = service.cancel(job_id, reason)
            except NotFound:
                self._send_not_found()
                return
            except InvalidInput as error:
                self._send_json_payload(
                    {"error": {"message": str(error), "type": "invalid_request"}},
                    status=400,
                )
                return
            if not records:
                self._send_not_found()
                return
            self._send_json_payload({
                "object": "job_cancel",
                "job": _job_record_payload(records[0]),
                "cancelled_count": len(records),
            })
            return
        model_route = _MODEL_REQUEST_FACADE.route(path)
        if model_route is not None:
            _serve_logger.debug(f"do_POST: model route matched, operation={model_route.operation!r}")
            if not context["authorized"]:
                if model_route.operation == "chat.completions":
                    record_early_chat_metric("unauthenticated")
                self._send_auth_error()
                return
            # Preserve the lifecycle admission precedence of the legacy
            # execution path: a draining runtime must answer DRAINING before
            # protocol normalization can report a client-side 400.
            lifecycle = sonder_lifecycle.get()
            if lifecycle.coordinator.draining:
                _serve_logger.error(f"request rejected: runtime is draining for shutdown, correlation={self._correlation()!r}")
                self._send_json_payload(
                    sonder_lifecycle.error_envelope(
                        "DRAINING",
                        "the runtime is draining for shutdown",
                        self._correlation(),
                        retryable=True,
                    ),
                    status=503,
                    headers={"Retry-After": "1"},
                )
                return
            # The facade is deliberately given the already-authenticated
            # request context as its policy hook.  Existing model-selection,
            # developer-authority, lifecycle, and control policy checks below
            # remain in force; this hook prevents a future caller from using
            # the codec as an unauthenticated generation bypass.
            facade = ModelRequestFacade(
                policy_hook=lambda _operation, _payload: bool(
                    context["authorized"]
                ),
            )
            try:
                facade_payload = dict(req)
                if model_route.operation == "chat.completions":
                    # Preserve the established adapter validation vocabulary
                    # before the provider-neutral facade normalizes the same
                    # envelope.
                    _validate_chat_messages(facade_payload.get("messages"))
                if "model" not in facade_payload:
                    facade_payload["model"] = "sonder"
                # The established chat adapter treats JSON null as the
                # omitted non-streaming default; preserve that compatibility
                # while the provider-neutral facade accepts strict booleans.
                if facade_payload.get("stream", False) is None:
                    facade_payload["stream"] = False
                normalized = facade.normalize(path, facade_payload)
            except HTTPRequestError as error:
                record_early_chat_metric("invalid_messages")
                self._send_json_payload(
                    {"error": {"message": error.message, "type": error.error_type}},
                    status=error.status,
                )
                return
            except ModelFacadeError as error:
                if model_route.operation == "chat.completions":
                    record_early_chat_metric("invalid_request")
                message = str(error)
                # Preserve the established HTTP adapter error vocabulary while
                # the root-free facade remains free to use its own taxonomy.
                if message == "model must be a non-empty string":
                    message = "model must be a string"
                elif message == "messages must be a non-empty array":
                    message = "messages must be an array"
                self._send_json_payload(
                    {"error": {"message": message, "type": "invalid_request"}},
                    status=error.status,
                )
                return
            model_operation = normalized.operation
            # Keep the established chat execution pipeline as the injected
            # adapter for both protocol envelopes.  Responses is model-only:
            # its input is translated to the same canonical chat messages, and
            # control/admin dispatch is disabled below for that operation.
            req = dict(req)
            req["model"] = normalized.model
            req["messages"] = [dict(message) for message in normalized.messages]
            req["stream"] = normalized.stream
            req.pop("input", None)
            if model_operation == "responses":
                path = "/v1/chat/completions"
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
            self._handle_permission_mode_post(req, context=context)
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
                _serve_logger.error(f"account registration failed, correlation={self._correlation()!r}", exc_info=True)
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
                _serve_logger.error(f"login failed, correlation={self._correlation()!r}", exc_info=True)
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
            # ``admin_set_account`` is also the REPL/MCP-facing legacy
            # wrapper, where a readable ``ERROR:`` string is the established
            # contract.  Do not carry that stringly failure over the direct
            # HTTP boundary as a successful 200 response: API clients need a
            # non-2xx status and the standard machine-readable error shape to
            # distinguish a rejected mutation from a completed one.
            from ...domain.cloud_access import has_legacy_error_prefix
            if has_legacy_error_prefix(out):
                self._send_json_payload(
                    {"error": {
                        "message": out.removeprefix("ERROR:").strip() or "account update failed",
                        "type": "invalid_request",
                    }},
                    status=400,
                )
                return
            self._send_json_payload({"ok": True, "message": out})
            return
        thin_handler = _THIN_HANDLERS.get(path)
        if thin_handler is not None:
            if not context["authorized"]:
                self._send_auth_error()
                return
            content_lengths = self.headers.get_all("Content-Length") or ()
            raw_length = content_lengths[0] if content_lengths else "0"
            length = int(raw_length) if raw_length.strip().isdigit() else 0
            raw_body = self.rfile.read(length) if length > 0 else b""
            self._request_body_consumed = True
            class _Req:
                method = "POST"
                def __init__(self, p, b, h):
                    self._path = p; self._body = b; self._headers = h
                @property
                def path(self): return self._path
                @property
                def body(self): return self._body
                @property
                def headers(self): return self._headers
            hdrs = {k: v for k, v in self.headers.items()}
            resp = thin_handler.handle(_Req(path, raw_body, hdrs))
            self._send_json_payload(resp.body, status=resp.status)
            return
        if path != "/v1/chat/completions":
            _serve_logger.debug(f"do_POST: unrecognized path={path!r}, returning 404")
            self._send_json_payload(
                {"error": {"message": "not found", "type": "not_found"}}, status=404
            )
            return

        _serve_logger.debug(f"do_POST: handling /v1/chat/completions")
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
        prompt = _last_user_message(messages)
        # Normalize an explicitly recognized whole-turn model request before
        # policy checks.  Otherwise ``use model x: /run ...`` could evade the
        # initial slash-command gate and execute after the rewrite below.
        # An explicit API model is a routing contract.  Do not reinterpret its
        # ordinary text as a fanout, ensemble, or named-model instruction;
        # direct MCP and the generation path apply the same precedence rule.
        natural_model = (
            server.natural_model_request(prompt)
            if not model_operation == "responses"
            and _uses_default_model_route(model) else None
        )
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
            # A single selected model rewrites the ordinary model prompt.
            # Fanout and ensemble wrappers remain intact until their dedicated
            # dispatch below; otherwise extracted text could be misclassified
            # by feedback/work-intent handlers and execute before gating.
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
        include_stream_usage = False
        if "stream_options" in req:
            stream_options = req["stream_options"]
            if not isinstance(stream_options, dict):
                record_early_chat_metric("invalid_stream_options")
                self._send_json_payload(
                    {"error": {
                        "message": "stream_options must be an object",
                        "type": "invalid_request",
                    }},
                    status=400,
                )
                return
            include_usage = stream_options.get("include_usage", False)
            if not isinstance(include_usage, bool):
                record_early_chat_metric("invalid_stream_options")
                self._send_json_payload(
                    {"error": {
                        "message": "stream_options.include_usage must be a boolean",
                        "type": "invalid_request",
                    }},
                    status=400,
                )
                return
            include_stream_usage = include_usage
        if natural_model and natural_model["kind"] in ("fanout", "ensemble"):
            # These routes spend several model calls. Local-open keeps its
            # single-user/full-tool behavior; shared deployments require the
            # same developer authority as the explicit /ensemble command.
            if not _developer_authorized(context):
                record_early_chat_metric("ensemble_forbidden" if natural_model["kind"] == "ensemble" else "fanout_forbidden")
                self._send_json_payload(
                    {"error": {"message": "developer or admin authentication is required for model ensembles", "type": "forbidden_command"}},
                    status=403,
                )
                return
        if natural_model and natural_model["kind"] == "model":
            model = natural_model["model"]
        model_selector = _request_model_selector(model)
        _serve_logger.debug(f"do_POST: model={model!r}, selector={model_selector!r}, stream={req.get('stream', False)}")
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
            _serve_logger.error(f"model prewarm failed for selector={model_selector!r}", exc_info=True)
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
        state, state_pinned = _acquire_http_conversation_state(
            context,
            session,
            token=_request_account_token(context, auth_header, account_header),
            pin=True,
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
        # Selecting a concrete model is an API routing contract.  The default
        # route retains Sonder's slash, feedback, web, and work conveniences;
        # an explicit target receives the caller's text unchanged instead of
        # allowing any of those local dispatchers to consume it.
        allow_control_routes = (
            model_operation != "responses"
            and _uses_default_model_route(model)
        )
        try:
            # SPEC-2 WP4 admission: bounded concurrency slot with queue
            # depth, admission deadline, drain and maintenance awareness,
            # bounded per-owner so one account cannot starve the rest.
            with _lifecycle.acquire_request_slot(
                mutating=True, owner=_admission_request_owner(context)
            ), state.lock:
                _record_chat("user", prompt, state=state)
                with server.activity_tracker.response_span(
                    "chat:%s" % (model or "sonder"),
                    prompt,
                    surface="http",
                    model=model,
                    session=storage_session,
                    project=storage_project,
                    reasoning_owner=_reasoning_request_owner(context),
                    feed_owner=_feed_request_owner(context),
                ) as activity_response:
                    if structured_schema is not None:
                        turn = _run_structured_prompt(
                            prompt, history, model_selector, structured_schema,
                            context_size=context_size, metrics=_lifecycle.metrics,
                        )
                        content = turn.content
                        response_model = turn.resolved_model
                        response_tier = turn.resolved_tier
                    elif allow_control_routes:
                        reply = _handle_slash(
                            prompt, messages=messages, state=state,
                            project=storage_project, context=context,
                            idempotency_key=self.headers.get("Idempotency-Key", ""),
                        )
                    if (structured_schema is None and allow_control_routes and reply is None
                            and not context.get("account")
                            and not (natural_model and natural_model["kind"] in ("fanout", "ensemble"))):
                        reply = _handle_feedback(prompt, state=state)
                    if (structured_schema is None and allow_control_routes and reply is None
                            and _developer_authorized(context)
                            and not (natural_model and natural_model["kind"] in ("fanout", "ensemble"))):
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
                    if structured_schema is None and reply is None and natural_model and natural_model["kind"] == "ensemble":
                        reply = server.ensemble_answer(
                            natural_model["prompt"], tiers=natural_model["tiers"],
                            project=storage_project, require_all_tiers=True,
                        )
                    if structured_schema is None and allow_control_routes and reply is None:
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
                                and _http_server_location_lookup_allowed(context)
                            ),
                        )
                        web_routed = reply is not None
                    if structured_schema is None and allow_control_routes and reply is None:
                        reply = _handle_work_intent(
                            prompt,
                            project=_work_project_for_request(project, storage_project),
                            authorized=_developer_authorized(context),
                            context=context,
                            idempotency_key=self.headers.get("Idempotency-Key", ""),
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
                            # The deterministic request cache is offered only
                            # to this plain, non-streaming generation
                            # fall-through: every control/tool/web/work/agent
                            # route has already returned above, and streamed
                            # or structured turns never receive a scope.  An
                            # empty scope is an unconditional cache denial.
                            cache_scope=(
                                "" if stream else _request_cache_scope(context)
                            ),
                            # Account-backed deployments intentionally do not
                            # inject or train the legacy global lesson store.
                            # Their durable chat/session project IDs remain
                            # principal-namespaced above.
                            augment=not bool(context.get("account")),
                            metrics=_lifecycle.metrics,
                        )
                        content = turn.content
                        response_iid = turn.iid
                        response_reasoning = turn.thinking
                        response_model = turn.resolved_model
                        response_tier = turn.resolved_tier
                # OpenAI-compatible content is the answer only.  Observable
                # execution data is returned separately in the bounded
                # ``sonder_activity`` vendor extension, never appended where
                # clients would replay it as assistant text.
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
                if (
                    turn is not None
                    and reply is None
                    and not web_routed
                    and not execution_routed
                ):
                    _capture_live_session_turn(
                        session_id=storage_session,
                        prompt=prompt,
                        history=history,
                        model=response_model or model_selector,
                        content=content,
                        request_id=self._correlation(),
                        turn_id=response_iid or uuid.uuid4().hex,
                        stream=stream,
                    )
        except sonder_lifecycle.AdmissionRejected as rejection:
            _serve_logger.error(f"request admission rejected: code={rejection.code!r}, retryable={rejection.retryable}, correlation={self._correlation()!r}")
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
            _serve_logger.error(f"model call error: kind={error.kind!r}, status={error.status}, detail={error.detail!r}, correlation={self._correlation()!r}")
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
        except _LiveSessionCaptureFailure:
            _serve_logger.error(f"durable session capture failed, correlation={self._correlation()!r}", exc_info=True)
            self._record_chat_completion_metric(
                _lifecycle, "session_capture_failed",
                getattr(self, "_request_started", _request_started),
            )
            self._send_json_payload(
                {"error": {
                    "message": "durable session capture unavailable",
                    "type": "server_error",
                    "code": "SESSION_CAPTURE_UNAVAILABLE",
                }},
                status=503,
                headers={"Retry-After": "1"},
            )
            return
        except Exception as error:
            _serve_logger.error(f"unhandled exception in chat completion handler, correlation={self._correlation()!r}", exc_info=True)
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
        finally:
            _release_http_conversation_state(state, state_pinned)

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
        # Present only when the turn actually consulted the deterministic
        # request cache; a closed "hit"/"miss" set with no request identity.
        if turn is not None and getattr(turn, "cache", ""):
            receipt["cache"] = turn.cache
        if stream:
            streamed = self._send_stream(
                content, model, iid=response_iid, elapsed_ms=elapsed_ms,
                receipt=receipt,
                usage=(
                    chat_formatting.chat_usage(activity_response)
                    if include_stream_usage else None
                ),
                activity_response=activity_response,
            )
            self._record_chat_completion_metric(
                _lifecycle,
                "ok" if streamed is True else
                "cancelled" if streamed is False else
                "stream_error",
                request_started,
            )
        else:
            try:
                if model_operation == "responses":
                    self._send_json(
                        facade.render_text(
                            model_operation,
                            content,
                            response_model or model,
                        ), elapsed_ms=elapsed_ms,
                    )
                else:
                    self._send_json(
                        _chat_completion_object(
                            content, model, iid=response_iid,
                            reasoning=response_reasoning, elapsed_ms=elapsed_ms,
                            receipt=receipt, activity_response=activity_response,
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

    def _send_stream(self, content, model, iid=None, elapsed_ms=None, receipt=None,
                     usage=None, activity_response=None):
        """Send one complete SSE response.

        ``True`` is a normal completed stream and ``False`` means the client
        left.  ``None`` is a server-side streaming failure after the response
        became SSE; callers must record it as a non-success result.  Model
        calls themselves complete before this method begins, so their errors
        retain the ordinary pre-header JSON error contract.
        """
        _serve_logger.debug(f"_send_stream: model={model!r}, content_len={len(content or '')}")
        iid = iid or uuid.uuid4().hex[:12]
        activity = (
            server.activity_tracker.public_response(
                activity_response, include_detail=False,
            )
            if isinstance(activity_response, dict)
            else (server.activity_tracker.public_snapshot(include_detail=False) or {}).get("latest")
        )
        headers_sent = False
        connection = getattr(self, "connection", None)
        if connection is not None:
            try:
                connection.settimeout(STREAM_IDLE_TIMEOUT_SECONDS)
            except (AttributeError, OSError):
                # Test probes and exotic socket wrappers may not expose a
                # writable timeout.  The handler's ordinary connection limit
                # still protects those paths.
                pass
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
            headers_sent = True
            self.wfile.write(_chunk(iid, model, {"role": "assistant", "content": content}).encode("utf-8"))
            self.wfile.write(_chunk(
                iid, model, {}, finish_reason="stop", elapsed_ms=elapsed_ms,
                receipt=receipt, activity=activity,
            ).encode("utf-8"))
            if usage is not None:
                self.wfile.write(_chunk(iid, model, {}, usage=usage).encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            # Header writes can fail before the first event just as body writes
            # can.  Return the same cancellation signal so the caller records
            # one truthful terminal metric instead of leaking a socket error.
            self.close_connection = True
            return False
        except Exception as error:
            # HTTP status and headers are immutable now.  Do not try to append
            # a JSON error body to an SSE response: a parser would see a bare
            # close or malformed event stream.  A functioning connection gets
            # a terminal, non-sensitive SSE error and [DONE] instead.
            _serve_logger.error(f"SSE stream response failed after headers_sent={headers_sent}", exc_info=True)
            self.log_error("stream response failed: %s", type(error).__name__)
            if headers_sent:
                Handler._send_stream_terminal_error(self, iid, model)
            self.close_connection = True
            return None

    def _send_stream_terminal_error(self, iid, model):
        """Best-effort terminal SSE error after an already-started stream."""
        payload = {
            "id": "chatcmpl-%s" % iid,
            "object": "error",
            "model": model,
            "error": {
                "message": "stream interrupted before completion",
                "type": "server_error",
                "code": "STREAM_INTERRUPTED",
            },
        }
        try:
            self.wfile.write(("data: %s\n\n" % json.dumps(payload)).encode("utf-8"))
            self.wfile.write(b"data: [DONE]\n\n")
            return True
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            return False
        except Exception as error:
            self.log_error("stream terminal error write failed: %s", type(error).__name__)
            return False


def main(config=None):
    _serve_logger.info("HTTP server starting")
    _serve_logger.debug("main: starting HTTP server")
    global CONFIGURED_PORT
    if config is not None:
        configure_typed_config(config)
    application = None
    if _SESSION_FACADE is None:
        from sonder_runtime.bootstrap.app import default_app
        from sonder_runtime.application.session.http_facade import HttpSessionFacade

        application = default_app(config=config)
        configure_session_facade(application.session_http_facade())
    if _CONTROL_PLANE_SERVICE is None:
        from sonder_runtime.bootstrap.app import default_app

        if application is None:
            application = default_app(config=config)
        configure_control_plane_service(application.control_plane_snapshot_service)
    port = _selected_listener_port(config)
    # Discovery reads the bound-listener value. Keep it synchronized when the
    # direct compatibility entrypoint overrides the typed configuration with a
    # positional argument or SONDER_PORT.
    CONFIGURED_PORT = port

    _validate_bind_security(HOST)
    lifecycle = sonder_lifecycle.get()
    try:
        # STARTING -> MIGRATING -> READY; no listener opens on failure.
        # When config is provided the caller (cmd_serve) already ran
        # migrate_all with the configured busy_timeout_ms — skip the
        # lifecycle's unconfigured duplicate.
        _serve_logger.info("Lifecycle startup initiated (STARTING -> MIGRATING -> READY)")
        lifecycle.startup(run_migrations=config is None)
        _serve_logger.info("Lifecycle startup completed")
    except Exception as error:
        _serve_logger.error("lifecycle startup failed before bind", exc_info=True)
        _serve_logger.critical("lifecycle startup failed before bind, server cannot start", exc_info=True)
        print("startup failed before bind: %s" % error, file=sys.stderr)
        raise SystemExit(1)
    lifecycle.begin_ollama_probe()
    try:
        httpd = ThreadingHTTPServer((HOST, port), Handler)
    except OSError:
        _serve_logger.critical(f"server cannot bind to {HOST}:{port}, port may already be in use", exc_info=True)
        raise
    # After a drain completes (signal or /v1/admin/drain), stop accepting.
    lifecycle.coordinator.add_flush_hook(
        lambda: threading.Thread(
            target=httpd.shutdown, daemon=True, name="sonder-httpd-shutdown"
        ).start()
    )
    global BOUND_PORT
    BOUND_PORT = port
    url = "http://%s:%d" % (HOST, port)
    _serve_logger.info(f"Server listening on {url}, auth_mode={_effective_auth_mode()!r}")
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
        _serve_logger.info("HTTP server shutting down")
        if not lifecycle.coordinator.draining:
            lifecycle.drain("server stopping")
        httpd.server_close()
        _serve_logger.info("HTTP server stopped")


if __name__ == "__main__":
    main()
