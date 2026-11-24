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

from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread
from sonder_runtime.platform.runtime_threads import run_bounded
import contextlib
import contextvars
import ipaddress
import os
import re
import sqlite3
import sys
import threading
import time
import urllib.parse
from collections import OrderedDict
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from sonder_runtime.interfaces.http.artifact_transfer import handle_artifact_transfer, is_artifact_route
from sonder_runtime.interfaces.http import web_app
from sonder_runtime.application.command_surface.command_text import slash_head as _slash_head
from sonder_runtime.interfaces.http.connection_limit import BoundedConnectionsMixin
from sonder_runtime.interfaces.http.host_policy import forwarded_client_ip, machine_host_names
from sonder_runtime.interfaces.http.memory_replication import (
    handle_memory_replication,
    is_memory_replication_route,
)
from sonder_runtime.interfaces.http import live_stream as _live_stream

_APP_CONTROL_BINDING = None
_APP_CONTROL_CONFIG = None
from sonder_runtime.interfaces.http.app_control import handle_app_control, is_app_control_route
from sonder_runtime.interfaces.http.work_runs import WorkRunner
from sonder_runtime.bootstrap.http_work_narration import WORK_RUN_OUTCOMES, classify_work_output, deferred_post as _deferred_post, enrich_work_record as _enrich_work_record, run_scoped as _run_work_scoped, work_ack as _work_ack, start_work as _start_narrated_work, admission_result as _work_admission_result
from sonder_runtime.bootstrap.work_narration import enrich_status as _narrated_status, cancel_linked_work as _cancel_linked_work
from sonder_runtime.bootstrap.http_command_narration import dispatch as _dispatch_narrated_command

_ARTIFACT_TRANSFER_BINDING = None
_ARTIFACT_TRANSFER_CONFIG = None
_SPANDA_CONFIG = None
_MEMORY_REPLICATION_RECEIVER = None
_MEMORY_REPLICATION_SERVICE = None
_ACCOUNT_LOGOUT_ADMISSION = threading.BoundedSemaphore(2)
from sonder_runtime.platform import context_policy
from sonder_runtime.platform.config_environment import env_bool

import logging as _logging_module
_serve_logger = _logging_module.getLogger(__name__)

from sonder_runtime.adapters.security.permission_policy import permission_policy

# Compatibility name for callers/tests that patch the old HTTP module seam;
# the provider object is shared with all canonical permission calls above.
permission_modes = permission_policy
import sonder_runtime.adapters.observability.activity_tracker as activity_tracker
import sonder_runtime.adapters.observability.chat_formatting as chat_formatting
import sonder_runtime.adapters.secrets as sonder_secrets
import sonder_runtime.adapters.web.lifecycle as sonder_lifecycle
from sonder_runtime.adapters.web import login_throttle_http
import sonder_runtime.platform.config as runtime_config
import sonder_runtime.platform.paths as runtime_paths
from sonder_runtime.adapters.command_catalog import command_catalog
from sonder_runtime.adapters import chat_effort as _chat_effort
from sonder_runtime.adapters import chat_temperature as _chat_temperature
from sonder_runtime.adapters.repl_services import (
    command_router as _command_router,
    personas as _personas,
)
from sonder_runtime.adapters.command_completion import (
    COMPLETE_DEFAULT_LIMIT as COMPLETE_DEFAULT_LIMIT,
    COMPLETE_MAX_LIMIT,
)
from sonder_runtime.adapters.command_completion import (
    completion_limit as _completion_limit,
)
from sonder_runtime.adapters.content_services import feedback, intents, training_tasks
from sonder_runtime.adapters.execution_tools import code_runner, grounding
from sonder_runtime.adapters.model_transport import ModelCallError
from sonder_runtime.adapters.execution import effect_fence
from sonder_runtime.adapters.inference import rpc_cluster, startup_prewarm
import sonder_runtime.adapters.cloud_model_refresh_schedule as cloud_model_refresh_schedule
from sonder_runtime.adapters.persistence import served_action_receipts
from sonder_runtime.adapters.persistence import http_work_runs
from sonder_runtime.adapters.security import role_tool_policy, unsafe_lab
from sonder_runtime.adapters.security.account_auth import account_auth as admin_auth
from sonder_runtime.adapters.web import live_reload
from sonder_runtime.interfaces.http import client_tools as _client_tools_http
from sonder_runtime.interfaces.http import media_routes as _media_routes
def _format_media_generation_error(kind, exc): return _media_routes.format_media_generation_error(kind, exc, logger=_serve_logger)
from sonder_runtime.application.extensions.facade import ExtensionAuthority
from sonder_runtime.application.context import bind_operation_context
from sonder_runtime.adapters import image_generation as _image_generation
from sonder_runtime.adapters import model3d_generation as _model3d_generation
import sonder_runtime.adapters.reference_lookup_lanes as _reference_lookup_lanes
from sonder_runtime.domain import launcher_health as sonder_health, public_policy, thinking_controls as _thinking_controls
from sonder_runtime.domain.common.errors import (
    Conflict,
    DependencyUnavailable,
    InvalidInput,
    NotFound,
)
from sonder_runtime.domain.launcher_health import token_is_configured
from sonder_runtime.domain.operational_capabilities import (
    build_operational_capabilities,
)
from sonder_runtime.interfaces.http.facades import HealthStatusFacade, permission_mode_post as _mode_post
from sonder_runtime.interfaces.http.facades.a2a import A2AAgentCardFacade
from sonder_runtime.interfaces.http.facades import client_protocol as _client_protocol
from sonder_runtime.interfaces.http.facades.client_protocol import (
    RECONNECT_ROUTE as _CLIENT_RECONNECT_ROUTE,
)
from sonder_runtime.interfaces.http.facades.a2a_jsonrpc import (
    build_application_a2a_handler as build_application_a2a_handler,
)
from sonder_runtime.interfaces.http.facades.control_plane import ControlPlaneFacade
from sonder_runtime.interfaces.http.facades.model_request import ModelRequestFacade
from sonder_runtime.interfaces.http.facades import observability_stream
from sonder_runtime.interfaces.http.facades import observatory_routes
from sonder_runtime.interfaces.http.facades import account_routes as _account_routes
from sonder_runtime.interfaces.http.facades import approval_routes as _approval_routes
from sonder_runtime.interfaces.http.facades import chat_helpers as _chat_helpers
from sonder_runtime.interfaces.http.facades import chat_routes as _chat_routes
from sonder_runtime.interfaces.http.facades import config_wiring as _config_wiring
from sonder_runtime.interfaces.http.facades import read_routes as _read_routes
from sonder_runtime.interfaces.http.facades import saas_auth as _saas_auth
from sonder_runtime.interfaces.http.facades import request_security as _request_security
from sonder_runtime.interfaces.http.facades import slash_helpers as _slash_helpers
from sonder_runtime.interfaces.http import thin_routes
from sonder_runtime.interfaces.http import chat_tool_stream as _chat_tool_stream
from sonder_runtime.platform import debug_dump

from . import authority_contract as tool_contract

_SERVE_MOD = sys.modules[__name__]
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


def configure_memory_replication_receiver(receiver):
    """Inject the explicitly configured peer receiver, or disable the route.

    No receiver is created by the HTTP adapter.  A host must construct one
    with its own durable sink, API key, and accepted source identities before
    this route becomes reachable.
    """
    if receiver is not None and not callable(getattr(receiver, "receive_bytes", None)):
        raise TypeError("memory replication receiver must provide receive_bytes")
    global _MEMORY_REPLICATION_RECEIVER
    _MEMORY_REPLICATION_RECEIVER = receiver
    _serve_logger.info(
        "Memory replication receiver configured: enabled=%s",
        receiver is not None,
    )
    return receiver


_UNSET = object()


def _detach_memory_replication_route(*, service=_UNSET, receiver=_UNSET):
    """Remove an HTTP exposure without taking ownership of its graph service.

    The service belongs to the composed Application.  Hosts use this helper on
    startup rollback and shutdown so a route cannot outlive its graph, while a
    caller with ``_close_default_resources=False`` remains the sole resource
    owner.  Identity matching prevents an old host from clearing a newer
    selection in a programmatic process.
    """
    global _MEMORY_REPLICATION_SERVICE, _MEMORY_REPLICATION_RECEIVER
    if service is _UNSET or _MEMORY_REPLICATION_SERVICE is service:
        _MEMORY_REPLICATION_SERVICE = None
    if receiver is _UNSET or _MEMORY_REPLICATION_RECEIVER is receiver:
        _MEMORY_REPLICATION_RECEIVER = None


def configure_memory_replication_service(service, *, close_replaced=True):
    """Install one already-owned local replication service at HTTP startup."""
    return _config_wiring.configure_memory_replication_service_impl(
        _SERVE_MOD, service, close_replaced=close_replaced,
    )


def configure_control_plane_service(service):
    """Inject the composed read-only operator snapshot service."""
    global _CONTROL_PLANE_SERVICE
    _CONTROL_PLANE_SERVICE = service
    _serve_logger.info("Control plane service configured for HTTP adapter")
    return service


_THIN_HANDLERS: dict = {}


def configure_thin_handlers(handlers: dict) -> dict:
    """Register SPEC-5 thin HTTP handlers by path."""
    global _THIN_HANDLERS
    _THIN_HANDLERS = dict(handlers)
    _serve_logger.info(f"SPEC-5 thin handlers configured: {sorted(_THIN_HANDLERS)}")
    return _THIN_HANDLERS


def _detach_thin_handlers(*, handlers=_UNSET) -> None:
    """Remove an exact HTTP handler mapping without clobbering a replacement."""
    global _THIN_HANDLERS
    if handlers is _UNSET or _THIN_HANDLERS is handlers:
        _THIN_HANDLERS = {}


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


_LiveSessionCaptureFailure = _config_wiring.LiveSessionCaptureFailure
_chat_routing_metadata = _config_wiring.chat_routing_metadata
_fail_live_session_request = _config_wiring.fail_live_session_request


def _capture_live_session_turn(
    *, session_id, prompt, history, model, content, request_id, turn_id, stream,
    provider_capture=None, selector=None, resolved_tier=None,
):
    from sonder_runtime.bootstrap.app import default_app

    return _config_wiring.capture_live_session_turn(
        session_id=session_id, prompt=prompt, history=history, model=model,
        content=content, request_id=request_id, turn_id=turn_id, stream=stream,
        provider_capture=provider_capture, selector=selector, resolved_tier=resolved_tier,
        default_app_fn=default_app, is_legacy_error_reply=_is_legacy_error_reply,
        logger=_serve_logger,
    )


_MAX_JOB_CANCEL_REASON = _config_wiring.MAX_JOB_CANCEL_REASON
_MAX_JOB_ID_LENGTH = _config_wiring.MAX_JOB_ID_LENGTH
_MAX_JOB_KIND_LENGTH = _config_wiring.MAX_JOB_KIND_LENGTH
_MAX_JOB_OPERATION_LENGTH = _config_wiring.MAX_JOB_OPERATION_LENGTH
_MAX_JOB_IDEMPOTENCY_LENGTH = _config_wiring.MAX_JOB_IDEMPOTENCY_LENGTH
_MAX_JOB_PARENT_LENGTH = _config_wiring.MAX_JOB_PARENT_LENGTH
_DuplicateJsonObjectKey = _config_wiring.DuplicateJsonObjectKey
_reject_duplicate_json_object_key = _config_wiring.reject_duplicate_json_object_key
_reject_nonfinite_json_number = _config_wiring.reject_nonfinite_json_number
_LOCAL_LOG_PAGE = _config_wiring.LOCAL_LOG_PAGE


def _local_server_log_tail():
    return _config_wiring.local_server_log_tail(
        default_home=runtime_paths.default_home,
        redact_text=activity_tracker._redact_text,
        tail_bytes=_LOCAL_LOG_TAIL_BYTES,
    )


def _request_route(path) -> str:
    """Return a normalized routing path while preserving query data elsewhere."""
    return urllib.parse.urlsplit(str(path or "")).path.rstrip("/") or "/"


_job_record_payload = _config_wiring.job_record_payload
_job_start_payload = _config_wiring.job_start_payload
_job_output_event_payload = _config_wiring.job_output_event_payload
_JOB_STREAM_DEFAULT_EVENTS = _config_wiring.JOB_STREAM_DEFAULT_EVENTS
_JOB_STREAM_MAX_EVENTS = _config_wiring.JOB_STREAM_MAX_EVENTS
_JOB_STREAM_DEFAULT_BYTES = _config_wiring.JOB_STREAM_DEFAULT_BYTES
_JOB_STREAM_MAX_BYTES = _config_wiring.JOB_STREAM_MAX_BYTES
_JOB_STREAM_MAX_AFTER = _config_wiring.JOB_STREAM_MAX_AFTER


def _job_cancel_id(path):
    return _config_wiring.job_cancel_id(path, unquote=urllib.parse.unquote)


def _compute_job_route(path):
    return _config_wiring.compute_job_route(path, unquote=urllib.parse.unquote)


def _job_subroute_id(path, suffix):
    return _config_wiring.job_subroute_id(path, suffix, unquote=urllib.parse.unquote)


def _job_record_id(path):
    return _config_wiring.job_record_id(path, unquote=urllib.parse.unquote)


def _job_stream_query(raw_query):
    return _config_wiring.job_stream_query(raw_query, parse_qs=urllib.parse.parse_qs)


def _local_log_dashboard_allowed(peer: str) -> bool:
    return not TLS_TERMINATED_BY_PROXY and _is_loopback_host(peer)


_env_int = _request_security.env_int
_env_flag = _request_security.env_flag
_parse_cors_origins = _request_security.parse_cors_origins


def _resolve_auth_mode(api_key="", require_account=False, configured=None):
    return _request_security.resolve_auth_mode(
        api_key, require_account, configured, logger=_serve_logger,
    )


# Auth + bind config. No credentials remains open only on loopback.
API_KEY = os.environ.get("SONDER_API_KEY", "")
AUTH_SECRET = os.environ.get("SONDER_AUTH_SECRET", "")
HOST = os.environ.get("SONDER_HOST", "127.0.0.1")
REQUIRE_ACCOUNT = _env_flag("SONDER_REQUIRE_ACCOUNT")
AUTH_MODE = _resolve_auth_mode(API_KEY, REQUIRE_ACCOUNT)
CORS_ORIGINS = _parse_cors_origins(os.environ.get("SONDER_CORS_ORIGINS", ""))
_WEB_APP = web_app.WebAppSettings.disabled()


def _parse_allowed_hosts(values):
    return _request_security.parse_allowed_hosts(values, logger=_serve_logger)


# Public names a proxy or remote client may put in ``Host`` besides loopback
# names and IP literals (DNS-rebinding defence; see host_policy.host_decision).
# They matter only in local-open mode: a listener that requires credentials
# accepts any well-formed name, since a rebinding page has no credentials.
ALLOWED_HOSTS = _parse_allowed_hosts(
    part for part in os.environ.get("SONDER_ALLOWED_HOSTS", "").split(",") if part.strip()
)
_MACHINE_NAME_TIMEOUT_SECONDS = 1.0
_MACHINE_HOST_NAMES = None
_MACHINE_HOST_NAMES_LOCK = threading.Lock()


def _compute_machine_host_names(timeout=_MACHINE_NAME_TIMEOUT_SECONDS):
    """This machine's own names, from ``gethostname``/``getfqdn`` only.

    ``getfqdn`` may consult the resolver, so it runs behind a bounded
    timeout; a slow or failing lookup costs the FQDN, never a request.  No
    other network lookup is made.
    """
    import socket

    try:
        hostname = socket.gethostname()
    except OSError:
        hostname = ""
    fqdn = ""
    if hostname:
        value, error, finished = run_bounded(
            lambda: socket.getfqdn(hostname), timeout, name="sonder-host-fqdn",
        )
        if finished and error is None and isinstance(value, str):
            fqdn = value
        else:
            _serve_logger.info("machine FQDN lookup did not finish; using the host name only")
    return machine_host_names(hostname, fqdn)


def _machine_host_names():
    """Computed once per process (startup warms it); never recomputed."""
    global _MACHINE_HOST_NAMES
    names = _MACHINE_HOST_NAMES
    if names is not None:
        return names
    with _MACHINE_HOST_NAMES_LOCK:
        if _MACHINE_HOST_NAMES is None:
            try:
                _MACHINE_HOST_NAMES = _compute_machine_host_names()
            except Exception:
                _serve_logger.warning("machine host names unavailable", exc_info=True)
                _MACHINE_HOST_NAMES = frozenset()
        return _MACHINE_HOST_NAMES


_REJECTED_HOST_LOG_INTERVAL_SECONDS = _request_security._REJECTED_HOST_LOG_INTERVAL_SECONDS
_REJECTED_HOST_LOG_MAX_NAMES_PER_INTERVAL = _request_security._REJECTED_HOST_LOG_MAX_NAMES_PER_INTERVAL
_REJECTED_HOST_LOG = OrderedDict()
_REJECTED_HOST_LOG_LOCK = threading.Lock()


def _log_rejected_host(value):
    _request_security.log_rejected_host(
        value, logger=_serve_logger, log_map=_REJECTED_HOST_LOG, lock=_REJECTED_HOST_LOG_LOCK,
    )


def _parse_observatory_origins(value):
    """Exact origins, spelled as a browser sends them (see normalize_origin)."""
    from sonder_runtime.platform.config import normalize_origins

    return frozenset(normalize_origins(
        sorted(_parse_cors_origins(value)), setting="SONDER_OBSERVATORY_ORIGINS",
    ))


# Route-scoped browser allowlist for the Observatory telemetry GET routes only
# (/.well-known/sonder-telemetry, /v1/observability/events,
# /v1/sonder/ecosystem).  CORS_ORIGINS is global and, in local-open mode, every
# route is admin, so an Observatory origin must never need to be added there.
OBSERVATORY_ORIGINS = _parse_observatory_origins(
    os.environ.get("SONDER_OBSERVATORY_ORIGINS", "")
)
# The validated serve entry point sets this whenever a TLS-terminating proxy
# fronts the otherwise-loopback runtime. Peer-address checks alone cannot tell
# that proxy apart from a direct local browser.
TLS_TERMINATED_BY_PROXY = _env_flag("SONDER_TLS_TERMINATED_BY_PROXY")
ALLOW_REGISTRATION = _env_flag("SONDER_ALLOW_REGISTRATION")
PUBLIC_HOSTING = _env_flag("SONDER_PUBLIC_HOSTING")
_CLIENT_TOOL_BROKER = _client_tools_http.ClientToolBroker()
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
# Interval of SSE keep-alive comment frames while a streamed turn generates.
STREAM_HEARTBEAT_SECONDS = max(1, min(300, _env_int("SONDER_STREAM_HEARTBEAT_SECONDS", 10)))
# [server].prewarm_on_start: load the default chat model once the listener is bound.
PREWARM_ON_START = env_bool(os.environ.get("SONDER_PREWARM_ON_START", "1"))
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
PERSONA = "auto"  # auto | off | <persona name>
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


_TELEMETRY_ROUTES = observability_stream.TELEMETRY_ROUTES


def _live_telemetry_application(*, build=False):
    """The application that owns live telemetry, without composing one to ask.

    Chat turns only describe themselves when a graph already exists; the
    admin telemetry routes may build the default graph like /trace does.
    """
    from sonder_runtime.bootstrap.app import built_default_app, default_app

    application = built_default_app()
    if application is None:
        try:
            application = getattr(server, "_APP_GRAPH", None)
        except Exception:
            # No legacy runtime injected (a bare HTTP host): nothing is bound.
            application = None
    if application is None and build:
        try:
            application = default_app()
        except Exception:
            _serve_logger.error("telemetry route could not compose the application", exc_info=True)
            return None
    return application


_is_legacy_error_reply = observatory_routes.is_legacy_error_reply
_chat_turn_outcome = observatory_routes.chat_turn_outcome
_register_telemetry_drain = observatory_routes.register_telemetry_drain


def _socket_peer_closed(connection):
    """True when the peer closed ``connection``; never blocks, never writes.

    An idle telemetry stream otherwise learns of a disconnect only when a
    heartbeat write fails, holding a subscriber slot for up to two
    heartbeats.  A readable socket whose peek returns no bytes is at EOF.
    Unread request bytes (a pipelined request) are not a close.
    """
    if connection is None:
        return False
    import select
    import socket as _socket

    try:
        if hasattr(select, "poll"):
            poller = select.poll()
            poller.register(connection, select.POLLIN | select.POLLHUP | select.POLLERR)
            ready = poller.poll(0)
            if not ready:
                return False
            if ready[0][1] & (select.POLLHUP | select.POLLERR) and not ready[0][1] & select.POLLIN:
                return True
        else:
            readable, _w, _x = select.select([connection], [], [], 0)
            if not readable:
                return False
        return connection.recv(1, _socket.MSG_PEEK) == b""
    except (BlockingIOError, InterruptedError, ValueError, TimeoutError):
        return False
    except OSError:
        return True


def _runtime_loopback_base_url():
    """The listener URL a same-host client uses (127.0.0.1 for 0.0.0.0)."""
    return observatory_routes.loopback_base_url(HOST, BOUND_PORT or CONFIGURED_PORT)


def _ecosystem_document(application, feed):
    """GET /v1/sonder/ecosystem body, or None when there is nothing to report."""
    from datetime import datetime, timezone
    import platform as _platform

    from sonder_runtime.platform.version import VERSION

    return observatory_routes.ecosystem_document(
        application, feed, base=_runtime_loopback_base_url(), version=VERSION,
        generated_at=datetime.now(timezone.utc), node=_platform.node(),
        observatory_origins=OBSERVATORY_ORIGINS | CORS_ORIGINS,
        dedicated_origins=OBSERVATORY_ORIGINS,
    )


ConversationState = _config_wiring.ConversationState
TurnResult = _config_wiring.TurnResult
_LegacyConversationState = _config_wiring.LegacyConversationState
_LEGACY_STATE = _LegacyConversationState(_SERVE_MOD)
HTTP_SESSION_STATE_LIMIT = max(2, min(
    1024, _env_int("SONDER_HTTP_SESSION_STATE_LIMIT", 128)
))
HTTP_SESSION_STATE_OWNER_LIMIT = max(1, min(
    HTTP_SESSION_STATE_LIMIT - 1,
    _env_int("SONDER_HTTP_SESSION_STATE_OWNER_LIMIT", 32),
))


def configure_typed_config(config) -> None:
    """Bind validated ``SonderConfig`` values at the HTTP boundary."""
    from sonder_runtime.adapters.security.control_plane_paths import (
        ControlPlanePaths,
        live_control_plane_inventory,
    )
    from sonder_runtime.adapters.web import listener_probe
    from sonder_runtime.bootstrap.app import default_app
    from sonder_runtime.bootstrap.app_control_http import AppControlBinding
    from sonder_runtime.bootstrap.artifact_transfer import ArtifactTransferBinding

    _config_wiring.apply_typed_config(
        _SERVE_MOD,
        config,
        artifact_binding_cls=ArtifactTransferBinding,
        app_control_binding_cls=AppControlBinding,
        control_plane_paths_cls=ControlPlanePaths,
        live_control_plane_inventory_fn=live_control_plane_inventory,
        default_app_fn=default_app,
        listener_probe_configure=listener_probe.configure_typed_config,
        web_app_root_fn=runtime_config.web_app_root,
    )
_HTTP_SESSION_STATES = OrderedDict()
_HTTP_SESSION_STATES_LOCK = threading.RLock()


def _state_or_legacy(state):
    return state if state is not None else _LEGACY_STATE


_state_principal = _request_security.state_principal
_account_identity = _request_security.account_identity
_build_principal = _request_security.build_principal


def _mode_principal(context):
    return _saas_auth.mode_principal(context, _state_principal)


def _bind_request_build_principal(context) -> None:
    from sonder_runtime.bootstrap.build_tools import bind_build_brief_principal

    authorized = bool((context or {}).get("authorized"))
    bind_build_brief_principal(_build_principal(context) if authorized else "")


_MAX_IDEMPOTENCY_KEY_LENGTH = _request_security.MAX_IDEMPOTENCY_KEY_LENGTH
IdempotencyRefusal = _request_security.IdempotencyRefusal
_idempotency_refusal_payload = _request_security.idempotency_refusal_payload
_send_idempotency_refusal = _request_security.send_idempotency_refusal


def _request_idempotency_key(context, endpoint, supplied_key):
    return _request_security.request_idempotency_key(
        context, endpoint, supplied_key, principal_of=_state_principal,
    )


def _http_action_idempotency_key(context, supplied_key, action):
    return _request_security.http_action_idempotency_key(
        context, supplied_key, action, principal_of=_state_principal,
    )


def _receipt_owner_scope(context):
    return _request_security.receipt_owner_scope(context, principal_of=_state_principal)


def _http_action_binding(context, supplied_key, action):
    return _request_security.http_action_binding(
        context, supplied_key, action, principal_of=_state_principal,
    )


def _idempotent_http_action(context, supplied_key, action, factory):
    return _request_security.idempotent_http_action(
        context, supplied_key, action, factory,
        serve_mod=_SERVE_MOD, receipts_store=served_action_receipts,
        lifecycle_get=sonder_lifecycle.get, storage_errors=(OSError, sqlite3.Error),
    )


def _task_account_scope(context):
    return _request_security.task_account_scope(context, principal_of=_state_principal)


_SCOPED_TASK_TOOLS = _request_security.SCOPED_TASK_TOOLS
_ACCOUNT_UNSCOPED_TASK_TOOLS = _request_security.ACCOUNT_UNSCOPED_TASK_TOOLS
_ACCOUNT_UNSCOPED_SAVED_WORKFLOW_TOOLS = _request_security.ACCOUNT_UNSCOPED_SAVED_WORKFLOW_TOOLS
_ACCOUNT_UNSCOPED_LOOP_ACTIONS = _request_security.ACCOUNT_UNSCOPED_LOOP_ACTIONS

# Kept as an explicit literal frozenset for AST boundary tests.
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
    "playbook_note",
    "playbook_read",
))


def _account_global_memory_refusal(tool, context):
    return _request_security.account_global_memory_refusal(
        tool, context, global_memory_tools=_ACCOUNT_GLOBAL_MEMORY_TOOLS,
    )


def _account_task_boundary_refusal(tool_name, kwargs, context):
    return _request_security.account_task_boundary_refusal(
        tool_name, kwargs, context, task_scope_of=_task_account_scope,
    )


def _served_task_tool(tool_name, kwargs, context):
    """Run account-scoped task operations without exposing a scope argument."""
    scope = _task_account_scope(context)
    if scope is not None:
        return server.scoped_task_tool_dispatch(tool_name, kwargs, account_scope=scope)
    handler = getattr(server, tool_name)
    return str(handler(**kwargs))


def _fanout_request_owner(context):
    return _request_security.fanout_request_owner(context, principal_of=_state_principal)


def _reasoning_request_owner(context):
    return _request_security.reasoning_request_owner(
        context,
        authenticates_callers=server._deployment_authenticates_callers,
        principal_of=_state_principal,
    )


def _admission_request_owner(context):
    return _request_security.admission_request_owner(
        context,
        authenticates_callers=server._deployment_authenticates_callers,
        principal_of=_state_principal,
    )


def _request_cache_scope(context):
    return _request_security.request_cache_scope(context, principal_of=_state_principal)


_CLIENT_PROTOCOL_SLOT = _client_protocol.ClientProtocolHostSlot(
    lambda: {"permission_mode": permission_policy.current_mode()})


def _client_protocol_host():
    """The client schema/reconnect host bound to the current application graph."""
    from sonder_runtime.bootstrap.app import default_app

    return _CLIENT_PROTOCOL_SLOT.bound_to(getattr(default_app(), "protocol", None))


def _observe_client_control_state():
    """Best-effort permission-mode publish on the client control stream."""
    _client_protocol.observe_control_state(_client_protocol_host, _serve_logger)


def _feed_request_owner(context):
    return _request_security.feed_request_owner(
        context,
        authenticates_callers=server._deployment_authenticates_callers,
        principal_of=_state_principal,
    )


_fanout_request_role = _request_security.fanout_request_role


def _prune_http_session_states(max_size=None):
    _request_security.prune_http_session_states(
        _HTTP_SESSION_STATES,
        HTTP_SESSION_STATE_LIMIT if max_size is None else max_size,
    )


def _evict_owner_lru_session_state(principal):
    return _request_security.evict_owner_lru_session_state(
        _HTTP_SESSION_STATES, principal,
    )


def _acquire_http_conversation_state(context, session, token="", *, pin=False):
    return _request_security.acquire_http_conversation_state(
        _SERVE_MOD, context, session, token=token, pin=pin,
    )


def _http_conversation_state(context, session, token=""):
    return _acquire_http_conversation_state(context, session, token)[0]


def _release_http_conversation_state(state, pinned):
    _request_security.release_http_conversation_state(
        _HTTP_SESSION_STATES_LOCK, state, pinned,
    )


_request_account_token = _request_security.request_account_token
_http_scope_value = _request_security.http_scope_value


def _hosted_storage_id(context, value, kind):
    return _request_security.hosted_storage_id(
        context, value, kind, principal_of=_state_principal,
    )


def _auth_response_extra_fields(account):
    return server._http_chat_tools().auth_response_extra_fields(account, public_hosting=bool(PUBLIC_HOSTING))


def _capabilities_payload(context=None):
    return server._http_chat_tools().capabilities_payload(
        context, auth_mode=_effective_auth_mode(), public_hosting=bool(PUBLIC_HOSTING),
        allow_registration=bool(ALLOW_REGISTRATION), require_account=bool(REQUIRE_ACCOUNT),
        admin_authorized=bool(context and _admin_authorized(context)),
    )


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


check_auth = _request_security.check_auth
_bearer_token = _request_security.bearer_token


def _auth_account(auth_header):
    return _saas_auth.resolve_account_token(
        _bearer_token(auth_header), _legacy_runtime(), admin_auth,
    )


def _app_control_deployment_authorized(authorization, config):
    return _request_security.app_control_deployment_authorized(
        authorization, config, previous_key_valid=sonder_secrets.previous_key_valid,
    )


def _app_managed_work_binding():
    service = getattr(_APP_CONTROL_BINDING, "_work_binding", None)
    if service is not None:
        service.require_current()
    return service


def _effective_auth_mode():
    return _request_security.effective_auth_mode(_SERVE_MOD)


def _warn_if_local_open():
    return _request_security.warn_if_local_open(_SERVE_MOD)


def _auth_context(auth_header="", account_header=""):
    return _request_security.auth_context(
        _SERVE_MOD, auth_header, account_header,
        previous_key_valid=sonder_secrets.previous_key_valid,
    )


def _authorized(auth_header, account_header=""):
    return _auth_context(auth_header, account_header)["authorized"]


def _developer_authorized(context):
    return _request_security.developer_authorized(context, admin_auth=admin_auth)


def _execution_feed_detail_allowed(context):
    return bool(
        server.activity_tracker.detail_enabled()
        and context.get("mode") != "local-open"
        and _developer_authorized(context)
    )


def _admin_authorized(context):
    return _request_security.admin_authorized(context, admin_auth=admin_auth)


def _restricted_tool_role(context):
    return _request_security.restricted_tool_role(
        context, developer_authorized_fn=_developer_authorized, role_user=public_policy.ROLE_USER,
    )


def _role_tool_refusal_reason(tool, context):
    role = _restricted_tool_role(context)
    return role_tool_policy.refusal_reason(tool, role) if role else ""


# Operations that alter shared runtime authority or persist privileged state.
# Keep this at the HTTP boundary: hiding a command in the app does not stop a
# crafted request, and a prompt must never be the thing that confers a role.
SYSTEM_OPERATION_ROLES = {
    "inference_pool_administration": "admin",
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
    "ollama_pool_admin_status": "inference_pool_administration",
    "permission_mode": "permission_mode_change",
    "permission_rule_set": "permission_rule_change",
    "permission_approve": "permission_rule_change",
    "elevate": "permission_mode_change",
    "runtime_policy_update": "runtime_policy_change",
    "cloud_opt_in": "runtime_policy_change",
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
    "autopilot_cancel": "automation_lifecycle", "mission_start": "automation_lifecycle",
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
    # A caller's judgement of a delegated run is the operator's review.
    "record_outcome": "workspace_execution",
}


def _http_system_operation_for(tool):
    return tool_contract.system_operation_for(
        tool,
        operation_tools=SYSTEM_OPERATION_TOOLS,
        operator_tools=getattr(server, "_AGENT_SYSTEM_OPERATOR_TOOLS", ()),
        canonicalize=getattr(server, "_canonical_agent_tool_name", None),
    )


def _ollama_pool_admin_page(context, params):
    """Authorize before touching pool state, for both HTTP entry paths."""
    if not _admin_authorized(context):
        return {"error": "authorization"}, 403
    if not isinstance(params, dict) or set(params) - {"refresh", "cursor", "page_size"}:
        return {"error": "invalid_request"}, 400
    result = server._ollama_pool_admin_status_data(principal=_state_principal(context), **params)
    return result, 400 if "error" in result else 200


_http_approver = _approval_routes.approver_label  # who issued an HTTP approval


def _audit_http_approval(action, approval, started):
    _request_security.audit_http_approval(
        action, approval, started, runtime=server, logger=_serve_logger,
    )


_account_created_message = _request_security.account_created_message
_is_loopback_host = _request_security.is_loopback_host
_require_exact_bind_values = _request_security.require_exact_bind_values


def _system_operation_authority_error(operation, context):
    return _request_security.system_operation_authority_error(
        operation, context,
        operation_roles=SYSTEM_OPERATION_ROLES,
        admin_authorized_fn=_admin_authorized,
        developer_authorized_fn=_developer_authorized,
    )


def _keyless_loopback_auth_mode(auth_mode, api_key, host):
    return _request_security.keyless_loopback_auth_mode(
        auth_mode, api_key, host, logger=_serve_logger,
    )


def _a2a_discovery_base_url():
    return _request_security.a2a_discovery_base_url(_SERVE_MOD)


def _selected_listener_port(config=None, argv=None):
    return _request_security.selected_listener_port(DEFAULT_PORT, config=config, argv=argv)


def _http_server_location_lookup_allowed(context):
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
    allow_non_loopback_proxy_backend=None,
):
    _request_security.validate_bind_security(
        host,
        api_key=api_key,
        auth_mode=auth_mode,
        auth_secret=auth_secret,
        tls_terminated_by_proxy=tls_terminated_by_proxy,
        allow_non_loopback_proxy_backend=allow_non_loopback_proxy_backend,
        min_api_key_length=runtime_config.MIN_API_KEY_LENGTH,
        min_account_secret_length=MIN_ACCOUNT_SECRET_LENGTH,
        unsafe_lab_require_startup=unsafe_lab.require_startup,
        serve_mod=_SERVE_MOD,
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
    # Shows operator prompt overrides, host environment/hardware, and state-home paths.
    "/prompts", "/persona", "/env", "/environment", "/hardware",
    "/filepolicy", "/files", "/find", "/read", "/write", "/append", "/edit",
    "/delete", "/master", "/master_orchestrate", "/delegate", "/pass", "/good", "/accept", "/accepted", "/used",
    "/copied", "/edited", "/fail", "/bad", "/trace", "/strict", "/run",
    "/runwindow", "/runnew", "/runconsole", "/runproject", "/train", "/learn",
    "/asset", "/assets", "/assetgen", "/artifact", "/forge", "/gamesuite",
    "/game", "/gamegen", "/gamefleet", "/gamecampaign",
    "/activity", "/tools", "/work", "/agent", "/report", "/endreport",
    "/checklist", "/plan", "/inventory", "/workspace", "/tree", "/folders", "/search", "/grep",
    "/programs", "/programfind", "/scripts", "/scriptfind", "/image", "/img", "/gif", "/3d", "/model3d",
    "/inspectimage", "/doc", "/document_extract", "/media", "/media_inspect",
    "/loc", "/code_metrics", "/schema", "/schema_validate",
    "/languages", "/language_matrix", "/calc", "/math_eval",
    "/logic", "/logic_verify", "/regex", "/regex_lab",
    "/graph", "/code_graph", "/sdiff", "/diff_semantic",
    "/reason", "/reasoning_board", "/symcheck", "/symbol_verify",
    "/citecheck", "/citation_audit", "/crosscheck", "/fact_cross_check",
    "/consensus", "/consistency_check", "/fallback", "/plan_fallback",
    "/resilience", "/runtime_resilience_check", "/shield", "/prompt_shield",
    "/diagnose", "/error_diagnose", "/audit", "/code_audit",
    "/transform", "/data_transform", "/learntool", "/learn_tool", "/discovertool",
    "/vision", "/analyzeimage", "/vision_analyze",
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
    return _slash_helpers.dangerous_http_slash(
        content, dangerous_commands=DANGEROUS_HTTP_SLASH_COMMANDS,
    )


# Slash names gated by action rather than by membership in the frozenset above.
_CONDITIONALLY_GATED_SLASH = frozenset({
    "/autopilot", "/auto", "/runtime", "/models", "/update", "/updatecheck", "/updatesource", "/stash", "/runtime-stash", "/selfmod", "/selfmodify",
})

_DEVELOPER_AUTHORITY_BY_MODE = _request_security.DEVELOPER_AUTHORITY_BY_MODE


def _deployment_gating_summary():
    return _request_security.deployment_gating_summary(_SERVE_MOD)


HTTPRequestError = _config_wiring.HTTPRequestError


def _strip_footer(text):
    return _slash_helpers.strip_footer(text, runtime=server)


_strip_trace = _slash_helpers.strip_trace


def _answer_only(text):
    return _strip_trace(_strip_footer(text or "")).rstrip()


def _record_chat(role, content, kind="message", state=None):
    _slash_helpers.record_chat(_SERVE_MOD, role, content, kind=kind, state=state)


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


_on_off = _slash_helpers.on_off
_SUPPORTED_CHAT_ROLES = _slash_helpers.SUPPORTED_CHAT_ROLES
_validate_chat_messages = _slash_helpers.validate_chat_messages
_chat_facade_payload = _slash_helpers.chat_facade_payload
_last_user_message = _slash_helpers.last_user_message


def _history_from_messages(messages):
    return _slash_helpers.history_from_messages(
        messages, runtime=server, answer_only_fn=_answer_only,
    )


SERVER_SIDE_HISTORY_TURNS = _slash_helpers.SERVER_SIDE_HISTORY_TURNS
_CHAT_HISTORY_SOURCES = _slash_helpers.CHAT_HISTORY_SOURCES


def _durable_session_history(storage_session, limit):
    return _slash_helpers.durable_session_history(
        storage_session, limit,
        session_facade=_SESSION_FACADE, strip_footer_fn=_strip_footer, logger=_serve_logger,
    )


def _server_side_history(storage_session, limit=SERVER_SIDE_HISTORY_TURNS):
    import sonder_runtime.adapters.memory_store as memory_store

    return _slash_helpers.server_side_history(
        _SERVE_MOD, storage_session, limit=limit, session_turns_fn=memory_store.session_turns,
    )


def _parse_train_n(arg):
    return _slash_helpers.parse_train_n(arg, default_n=TRAIN_DEFAULT_N, max_n=TRAIN_MAX_N)


def _parse_run_timeout(arg):
    return _slash_helpers.parse_run_timeout(arg, grounding_mod=grounding)


def _do_run(timeout=grounding.DEFAULT_TIMEOUT):
    return _do_run_from_messages(timeout=timeout, state=_LEGACY_STATE)


def _run_sources_from_messages(messages=None, state=None):
    return _slash_helpers.run_sources_from_messages(_SERVE_MOD, messages, state=state)


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
        block["code"], language=block["language"], timeout=timeout,
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
        block["code"], language=block["language"], timeout=timeout,
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
    from sonder_runtime.adapters import tool_lane_guard

    # Practice runs the code in the default route's reply: judge that route first.
    if refusal := tool_lane_guard.selector_refusal("", server.TIERS, server._RUNTIME_POLICY):
        return refusal
    tasks = training_tasks.sample(n)
    passed = 0
    lessons = 0
    lines = []
    for t in tasks:
        lines.append("  practicing: %s ..." % t["name"])
        resp = server.sonder(t["prompt"], temperature=0.2)  # practice keeps its fixed 0.2
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


def _http_slash_refusal(cmd, argument="", context=None, arguments=None):
    return _slash_helpers.http_slash_refusal(
        _SERVE_MOD, cmd, argument=argument, context=context,
        arguments=arguments, role_tool_policy=role_tool_policy,
    )


def _slash_call_arguments(cmd, argument):
    """The tool arguments a slash command will call with, or None."""
    from sonder_runtime.interfaces.http.facades import developer_chat
    return developer_chat.slash_call_arguments(cmd, argument)
# S1. Refusals observed during one chat turn. The permission gate reports
# every unattended decision to its observers; this one keeps the refusals
# that name a call (an effect-class tool with arguments, already noted as
# pending in the approval ledger) for the turn that is collecting them.
_CHAT_REFUSALS = contextvars.ContextVar("sonder_http_chat_refusals", default=None)
_MAX_TURN_REFUSALS = 8


def _capture_unattended_refusal(decision, _surface):
    sink = _CHAT_REFUSALS.get()
    if sink is None or len(sink) >= _MAX_TURN_REFUSALS:
        return
    if (
        getattr(decision, "action", "") == "deny"
        and getattr(decision, "source", "") == "unattended"
        and getattr(decision, "call_id", "")
    ):
        sink.append(decision)


def _ensure_refusal_observer():
    """Idempotent: the gate de-duplicates observers by identity."""
    try:
        permission_policy.add_decision_observer(_capture_unattended_refusal)
    except Exception:
        _serve_logger.warning("refusal observer unavailable; receipts omit refusal", exc_info=True)


def _loop_global_operation_refusal(actions_json, context=None, *, _depth=0):
    """Refuse HTTP loop payloads that would mutate shared runtime state."""
    return tool_contract.loop_global_operation_refusal(
        actions_json, context, loop_action_tool=server._loop_action_tool,
        system_operation_for=_http_system_operation_for, role_tool_refusal=_role_tool_refusal_reason,
        admin_authorized=_admin_authorized, authority_error=_system_operation_authority_error,
        nested_slash_refusal=_nested_slash_authority_refusal,
        account_global_memory_refusal=_account_global_memory_refusal, depth=_depth,
    )


def _http_tool_authority_refusal(tools, label, context=None):
    return _slash_helpers.http_tool_authority_refusal(
        _SERVE_MOD, tools, label, context=context,
        unbound_sentinel=tool_contract.SYSTEM_OPERATION_UNBOUND,
    )


def _http_tool_refusal(tools, label, context=None, arguments=None):
    return _slash_helpers.http_tool_refusal(
        _SERVE_MOD, tools, label, context=context, arguments=arguments,
    )


def _slash_system_operation(command, argument):
    return tool_contract.slash_system_operation(command, argument, slash_head=_slash_head)


def _nested_slash_authority_refusal(prompt, label, context, *, _depth=0):
    return tool_contract.nested_slash_authority_refusal(
        prompt, label, context, slash_head=_slash_head,
        slash_operation=_slash_system_operation, authority_error=_system_operation_authority_error,
        dangerous_slash=_dangerous_http_slash, developer_authorized=_developer_authorized,
        console_tools=command_catalog.console_tools, http_slash_tools=command_catalog.http_slash_tools,
        narrow_branch_tools=command_catalog.narrow_branch_tools,
        catalog_unavailable=command_catalog.CatalogUnavailable,
        tool_authority_refusal=_http_tool_authority_refusal,
        task_boundary_refusal=_account_task_boundary_refusal,
        parse_invocation=command_catalog.parse_invocation,
        project_refusal=server.served_project_refusal,
        loop_refusal=_loop_global_operation_refusal, depth=_depth,
    )


def _served_control_guard_for(context, *, disabled=False):
    return _slash_helpers.served_control_guard_for(_SERVE_MOD, context, disabled=disabled)


_BARE_SLASH_COMMAND = re.compile(r"/[A-Za-z][A-Za-z0-9_-]{0,63}")


def _http_principal(auth):
    """The principal an authenticated HTTP caller acts as, or "" when unknown.

    An account is its hashed identity; the owner key and the local-open
    listener are ``owner``. Shared by the typed developer routes and their
    chat spellings so both bind the same principal.
    """
    return _build_principal(auth if isinstance(auth, dict) else None)


def _http_workspace_roots(auth):
    """Configured [state].workspace_roots (resolved) for an admin; () otherwise.

    Never ``file_ops.lane_grant_roots``: its ambient roots are for local-owner lanes only."""
    if not _admin_authorized(auth):
        return ()
    from sonder_runtime.bootstrap.app import default_app
    from sonder_runtime.platform.policy_roots import try_parse_policy_root
    state = getattr(getattr(default_app(), "config", None), "state", None)
    resolved = (p.resolve() for r in getattr(state, "workspace_roots", ()) if (p := try_parse_policy_root(str(r) if isinstance(r, os.PathLike) else r, expand_user=False)) is not None)
    return tuple(dict.fromkeys(str(q) for q in resolved if try_parse_policy_root(str(q), expand_user=False) is not None))


def _http_debug_context(auth, correlation_id):
    """Typed debug-tool context: source=http, principal from the account."""
    principal = _http_principal(auth)
    if not principal:
        raise PermissionError("authenticated account identity is unavailable")
    context = sonder_lifecycle.get().operation_context(correlation_id, auth)
    roots = tuple(Path(root) for root in _http_workspace_roots(auth))
    from sonder_runtime.bootstrap.app import default_app
    from sonder_runtime.interfaces.orchestration_commands import inherited_remote_consent
    return replace(context, principal_id=principal, source="http", workspace_roots=roots, remote_ollama_allowed=inherited_remote_consent(context.remote_ollama_allowed, _admin_authorized(auth), default_app))


def _debug_tools_http_facade(application):
    """The admin crash/profile facade over ``application.debug_tools``.

    Host-launching routes are graded by the permission modes first, exactly
    as the typed gateway grades an HTTP caller.
    """
    from sonder_runtime.bootstrap.debug_tools import debug_http_authorizer
    from sonder_runtime.interfaces.http.facades.debug_tools import DebugToolsHttpFacade

    service = getattr(application, "debug_tools", None)
    return DebugToolsHttpFacade(lambda: service, authorize=debug_http_authorizer(service))


def _developer_chat_reply(cmd, arg, context):
    """``/test``, ``/digest``, ``/build``, ``/fix-build``, ``/crash``, ``/profile`` in chat.

    ``interfaces/http/facades/developer_chat.py`` turns the line into exactly
    the call its HTTP route makes, under that route's authority check. The
    permission modes have already graded the line unattended at the chain
    gate (``_http_slash_refusal``), without the call's arguments; a mode that
    refuses the run refuses it there, naming no call. Approval of one run by
    ``call_id`` goes through the HTTP route.
    """
    from sonder_runtime.bootstrap.app import default_app
    from sonder_runtime.interfaces.http.facades import developer_chat

    return developer_chat.reply(
        cmd, arg, context, admin_authorized=_admin_authorized,
        developer_authorized=_developer_authorized, principal_of=_http_principal,
        workspace_roots_of=_http_workspace_roots, application=default_app,
        debug_facade=_debug_tools_http_facade, debug_context=_http_debug_context,
    )


def _narrate_http_command(name, arguments, call, context):
    return _dispatch_narrated_command(server, name, arguments, call, runner=_WORK_RUNNER,
        principal=_state_principal(context), store=http_work_runs, tracker=activity_tracker,
        thread_wrapper=_bind_current_activity())


def _handle_slash(content, messages=None, state=None, project="", context=None,
                  idempotency_key="", workspace_project=None, lane_session="", storage_session="", selected_model=False):
    """Return response text if `content` is a recognized slash command, else None.  selected_model: a lone unknown "/word" goes to the model."""
    state = _state_or_legacy(state)

    stripped = (content or "").strip()
    if not stripped.startswith("/"):
        return None

    parts = stripped.split(None, 1)
    cmd = _slash_head(parts[0])
    arg = parts[1] if len(parts) > 1 else ""
    _serve_logger.debug("_handle_slash: slash command received")
    # One choke point in front of every branch of this ~130-branch chain (as in the REPL).
    run_args = server._http_chat_tools().slash_run_arguments(
        cmd, lambda: _run_sources_from_messages(messages, state=state), grounding.extract_runnable_code_block,
    )
    with permission_policy.capture_preapproved_scope() as preapproved:
        refusal = _http_slash_refusal(cmd, arg, context=context) if run_args is None else _http_slash_refusal(cmd, arg, context=context, arguments=run_args)
    if refusal:
        _serve_logger.debug("_handle_slash: slash command refused")
        return refusal
    operation = _slash_system_operation(cmd, arg)
    if operation and context is not None:
        authority_error = _system_operation_authority_error(operation, context)
        if authority_error:
            return "refused %s: %s" % (cmd, authority_error)

    def _control(**extra):
        with permission_policy.preapproved_tools_scope(preapproved):
            return server.control_command(stripped, project=project, **extra)

    if cmd == "/help":
        # The catalog derives every command from the dispatch chains and the
        # live tool registry, so /help cannot drift behind them the way the
        # hand-written text it replaced did.
        return command_catalog.help_text(arg.strip())
    if cmd == "/dump":
        return _dump_chat(
            messages=messages, label=arg.strip() or "chat", state=state
        )
    if cmd in ("/image", "/img"):
        return _media_routes.run_media_slash("image", arg.strip() or "a random image", _image_generation.generate_chat_image, logger=_serve_logger)
    if cmd == "/gif":
        return _media_routes.run_media_slash("gif", arg.strip() or "a random animated gif", _image_generation.generate_chat_gif, logger=_serve_logger)
    if cmd in ("/3d", "/model3d"):
        return _media_routes.run_media_slash("3d", arg.strip() or "a stylized glowing lantern", _model3d_generation.generate_chat_3d_model, logger=_serve_logger)
    if cmd == "/stats":
        return server.sonder_stats()
    if cmd == "/version":
        from sonder_runtime.platform import version as build_identity
        stamped = build_identity.stamped_build_info()
        if stamped is not None:
            return "sonder %s (commit %s, stamped release)" % (
                stamped.version, stamped.commit_sha[:12],
            )
        return "sonder %s" % build_identity.runtime_version()
    if cmd in ("/env", "/environment"):
        return server.environment_status(
            refresh=(arg or "").strip().lower() == "refresh",
        )
    if cmd == "/hardware":
        return server.hardware_profile(
            refresh=(arg or "").strip().lower() == "refresh",
        )
    if cmd == "/route":
        if not arg.strip():
            return "usage: /route <task or prompt>"
        return server.route_request(arg.strip())
    if cmd == "/context":
        return server.context_health()
    if cmd == "/prompts":
        return _control()
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
        return _control()
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
        return _control()
    if cmd in ("/activity", "/tools"):
        return server.activity_status()
    if cmd in ("/test", "/digest", "/build", "/fix-build", "/crash", "/profile"):
        return _developer_chat_reply(cmd, arg, context)
    if cmd in ("/autopilot", "/auto"):
        # Starting a persistent run is a side effect.  A caller may not know
        # whether its connection died before or after the controller accepted
        # it, so an opt-in replay key returns the first result rather than
        # creating a sibling run.
        return _idempotent_http_action(
            context, idempotency_key, "autopilot\0%s\0%s" % (project, stripped),
            lambda: _narrate_http_command("autopilot", {"action": arg.partition(" ")[0], "objective": arg.partition(" ")[2], "project": project}, lambda: _control(
                autopilot_request_owner=_task_account_scope(context),
            ), context),
        )
    if cmd in ("/runtime", "/models"):
        return _control()
    if cmd in ("/update", "/updatecheck", "/updatesource"):
        return _control()
    if cmd in ("/stash", "/runtime-stash"):
        return _control()
    if cmd in ("/selfmod", "/selfmodify"):
        return _control()
    if cmd in ("/goal", "/goals"):
        return _idempotent_http_action(
            context, idempotency_key, "goal\0%s\0%s" % (project, stripped),
            lambda: _control(),
        )
    if cmd in ("/mcp", "/convergence"):
        return _control()
    if cmd in ("/learning", "/learnhealth", "/metrics"):
        return _control()
    if cmd in ("/weather", "/forecast"):
        if not arg.strip():
            return "usage: /weather <city/state or ZIP>"
        return server.weather_lookup(arg.strip())
    if cmd in ("/work", "/agent"):
        return _chat_helpers.run_http_work_slash(
            arg, project=project, workspace_project=workspace_project, context=context,
            idempotency_key=idempotency_key, server=server,
            account_boundary_refusal=_account_task_boundary_refusal,
            idempotent_action=_idempotent_http_action, narrate_command=_narrate_http_command,
            fanout_owner=_fanout_request_owner, fanout_role=_fanout_request_role,
            developer_authorized=_developer_authorized,
            invoke=lambda prompt, max_steps, work_project: server.workbench_agent(
                prompt=prompt, tier="auto", max_steps=max_steps, project=work_project,
            ),
        )
    if cmd == "/mkdir":
        return _control()
    if cmd in (
        "/report", "/endreport", "/checklist", "/plan",
        "/inventory", "/workspace",
        "/tree", "/folders", "/search", "/grep",
        "/programs", "/programfind", "/scripts", "/scriptfind",
        "/image", "/inspectimage", "/doc", "/document_extract",
        "/media", "/media_inspect", "/loc", "/code_metrics",
        "/schema", "/schema_validate", "/languages", "/language_matrix",
        "/calc", "/math_eval", "/logic", "/logic_verify",
        "/regex", "/regex_lab", "/graph", "/code_graph",
        "/sdiff", "/diff_semantic", "/reason", "/reasoning_board",
        "/symcheck", "/symbol_verify", "/citecheck", "/citation_audit",
        "/crosscheck", "/fact_cross_check", "/consensus", "/consistency_check",
        "/fallback", "/plan_fallback", "/resilience", "/runtime_resilience_check",
        "/shield", "/prompt_shield", "/diagnose", "/error_diagnose",
        "/audit", "/code_audit", "/transform", "/data_transform",
        "/learntool", "/learn_tool", "/discovertool",
        "/vision", "/analyzeimage",
        "/runprogram", "/runscript",
        "/artifactcheck", "/verifyartifact", "/groundartifact",
    ):
        scope = _task_account_scope(context)
        if scope is not None and cmd in ("/checklist", "/plan"):
            if arg.strip():
                return server.scoped_task_tool_dispatch(
                    "checklist_show", {"checklist_id": arg.strip()}, account_scope=scope,
                )
            return server.scoped_latest_checklist(scope)
        return _control()
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
        # The token stays in the console session; the reply never shows it.
        from ...domain.login_output import split_login_output
        token, display = split_login_output(out)
        if token:
            state.token = token
            state.account = server._admin_account_from_token(state.token)
        return display
    if cmd == "/whoami":
        return server.admin_whoami(state.token, public_hosting=bool(PUBLIC_HOSTING))
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
    if cmd == "/delegate":
        from sonder_runtime.bootstrap.app import default_app
        from sonder_runtime.interfaces.http.agent_work_routes import delegate_chat
        return delegate_chat(
            arg, application=default_app(), context=_http_debug_context(context or {}, "delegate-chat"),
            project=project if workspace_project is None else workspace_project,
            policy=permission_policy, parent_session_id=lane_session, command_id=idempotency_key,
            allow_creation=_admin_authorized(context or {}),
            state_home_of=lambda app: getattr(getattr(getattr(app, "config", None), "state", None), "home", "") or runtime_paths.default_home(),
        )
    if cmd in ("/master", "/master_orchestrate"):
        task_boundary_error = _account_task_boundary_refusal("master_orchestrate", {}, context)
        if task_boundary_error:
            return task_boundary_error
        from sonder_runtime.interfaces.orchestration_commands import (
            execute_master_command, parse_master_arguments, resolved_agent_count, uses_tool_arguments,
        )
        if not uses_tool_arguments(arg):
            # One parser for every surface ([inline|delegate|fleet] [N] <task>,
            # 0 = capacity-sized), run through the narrated, idempotent start.
            try:
                parsed = parse_master_arguments(arg)
            except ValueError as error:
                return str(error)
            master_project = project if workspace_project is None else workspace_project
            return server.served_project_refusal(master_project) or _idempotent_http_action(
                context, idempotency_key, "master\0" + stripped,
                lambda: _narrate_http_command(
                    "master_orchestrate", {"task": parsed.task, "mode": parsed.mode, "agents": (count := resolved_agent_count(parsed, server.master_orchestrator.capacity))},
                    lambda: execute_master_command(
                        arg, orchestrate=server.master_orchestrate,
                        capacity=server.master_orchestrator.capacity, project=master_project, agents=count,
                    ),
                    context,
                ),
            )
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
    if cmd == "/temp":
        from sonder_runtime.interfaces.http.serve_policy import serve_lane_temperature
        return _chat_temperature.slash_temp(arg, storage_session, context, _state_principal, serve_lane_temperature())
    if cmd == "/effort":
        return _chat_effort.slash_effort(arg, storage_session, context, _state_principal)
    if cmd == "/persona":
        raw_arg = arg.strip()
        available = list(_personas.MODES) + _personas.names()
        if not raw_arg:
            return "persona: %s (available: %s)" % (
                state.persona or _personas.AUTO, ", ".join(available),
            )
        wanted = _personas.normalize_selection(raw_arg)
        if wanted is None:
            return "unknown persona %r; still %s (available: %s)" % (
                raw_arg, state.persona or _personas.AUTO, ", ".join(available),
            )
        state.persona = wanted
        if wanted == _personas.AUTO:
            return "persona: auto (auto-routes by tier and prompt)"
        if wanted == _personas.OFF:
            return "persona: off (no persona prompt injected)"
        return "persona: %s (pinned)" % wanted
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

    if _BARE_SLASH_COMMAND.fullmatch(stripped) and not selected_model:
        # A lone "/word" is a command attempt, not a sentence: answer without
        # spending a model call on a typo.  Same text as a hidden command so
        # the reply does not reveal which names exist for other accounts.
        return (
            "No command with that name is available to this account. "
            "Use /help to list the commands you can run."
        )
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
    return _slash_helpers.run_catalogued_tool_gated(
        _SERVE_MOD, line, tool_name, kwargs, handler, state=state, context=context,
    )


def _handle_feedback(content, state=None):
    return _slash_helpers.handle_feedback(_SERVE_MOD, content, state=state)


def _handle_intent(content, messages=None, state=None, context=None):
    return _slash_helpers.handle_intent(
        _SERVE_MOD, content, messages=messages, state=state, context=context,
    )


_HTTP_NATURAL_COMMANDS = _slash_helpers.HTTP_NATURAL_COMMANDS


def _handle_natural_command(
    content, *, messages=None, state=None, project="", context=None,
    idempotency_key="", workspace_project=None, lane_session="",
    storage_session="",
):
    return _slash_helpers.handle_natural_command(
        _SERVE_MOD, content, command_router=_command_router, messages=messages,
        state=state, project=project, context=context, idempotency_key=idempotency_key,
        workspace_project=workspace_project, lane_session=lane_session,
        storage_session=storage_session,
    )


def _work_project_for_request(project, storage_project, content="", auth=None):
    """The project routed natural work runs under for one served request.

    Durable state keeps its principal namespace; file work needs the original
    project selection so an unset/default project can use a creations lane.
    With no project, a directory the text names inside *auth*'s grant (an owner/admin's configured workspace roots, () otherwise) is used.
    """
    named = server.file_ops.named_directory(content, roots) if auth is not None and str(project or "").strip().lower() in {"", "default", "none"} and (roots := _http_workspace_roots(auth)) else ""
    return named or server.served_work_project(project) or (
        project or "default" if intents.classify_file_intent(content) else storage_project
    )


def _work_run_stop_reason():
    return _chat_helpers.work_run_stop_reason(sonder_lifecycle.get)


def _work_run_lifetime():
    return _chat_helpers.work_run_lifetime(sonder_lifecycle.get, _serve_logger)


_WORK_RUNNER = WorkRunner(
    store=http_work_runs, effects=effect_fence,
    wait_seconds=_env_int("SONDER_HTTP_WORK_WAIT_SECONDS", 240),
    budget_seconds=_env_int("SONDER_HTTP_WORK_BUDGET_SECONDS", 1800),
    max_running=_env_int("SONDER_HTTP_WORK_MAX_RUNNING", 2),
    max_live=_env_int("SONDER_HTTP_WORK_MAX_LIVE", 0),
    stop_reason=_work_run_stop_reason,
    lifetime=_work_run_lifetime,
    thread_factory=owned_runtime_thread,
)


def _work_run_record(result):
    return _chat_helpers.work_run_record(result, work_run_outcomes=WORK_RUN_OUTCOMES)


def _work_run_pending_text(run_id):
    return _chat_helpers.work_run_pending_text(run_id, budget_seconds=_WORK_RUNNER.budget_seconds)


def _bind_current_activity():
    return _chat_helpers.bind_current_activity(activity_tracker, _chat_effort)


def _append_session_transcript_event(session_id, event_type, content, *, turn_id, request_id="", extra=None):
    from sonder_runtime.bootstrap.app import default_app
    from sonder_runtime.domain.session_ref import is_valid_session_id

    _chat_helpers.append_session_transcript_event(
        session_id, event_type, content, turn_id=turn_id, request_id=request_id, extra=extra,
        default_app_fn=default_app, is_valid_session_id_fn=is_valid_session_id,
        redact_text=activity_tracker._redact_text, logger=_serve_logger,
    )


def _handle_work_intent(content, project="", authorized=False, context=None,
                        idempotency_key="", session_id="", session_ref="",
                        correlation_id="", with_receipt=False, tool_tier="",
                        record_transcript=False):
    from sonder_runtime.bootstrap.app import default_app

    return _chat_helpers.handle_work_intent(
        _SERVE_MOD, content, project=project, authorized=authorized, context=context,
        idempotency_key=idempotency_key, session_id=session_id, session_ref=session_ref,
        correlation_id=correlation_id, with_receipt=with_receipt, tool_tier=tool_tier,
        record_transcript=record_transcript, default_app_fn=default_app,
        work_ack_fn=_work_ack, run_work_scoped_fn=_run_work_scoped,
        classify_work_output_fn=classify_work_output,
        start_narrated_work_fn=_start_narrated_work,
        work_admission_result_fn=_work_admission_result,
        http_work_runs_mod=http_work_runs, activity_tracker_mod=activity_tracker,
        admission_rejected_cls=sonder_lifecycle.AdmissionRejected,
    )


def _model_to_tier(model):
    return _chat_helpers.model_to_tier(model, runtime=server)


_uses_default_model_route = _chat_helpers.uses_default_model_route


def _request_model_selector(model):
    return _chat_helpers.request_model_selector(_SERVE_MOD, model)


def _chat_model_selection_error(selector):
    return _chat_helpers.chat_model_selection_error(_SERVE_MOD, selector)


def _openai_model_rows():
    return _chat_helpers.openai_model_rows(
        _SERVE_MOD, gateway=getattr(_live_telemetry_application(), "model_gateway", None),
    )


_reasoning_audience = _chat_helpers.reasoning_audience


def _reasoning_visible_to(context):
    return _chat_helpers.reasoning_visible_to(_SERVE_MOD, context)


def _turn_reasoning():
    return _chat_helpers.turn_reasoning(server)


def _capture_http_provider_request(function):
    def _default_app():
        from sonder_runtime.bootstrap.app import default_app

        return default_app()

    return _chat_helpers.capture_http_provider_request(
        function, serve_mod=_SERVE_MOD, default_app_fn=_default_app,
    )


@_capture_http_provider_request
def _run_prompt(
    prompt, history=None, tier=None, context_size="", session="", project="",
    state=None, return_result=False, metrics=None, augment=True, cache_scope="",
    capture_request_id="", capture_turn_id="", capture_stream=False,
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
    from sonder_runtime.application.chat.honesty import guard_unsaved_file_claim
    from sonder_runtime.adapters.chat_file_routing import suggested_folder
    result = TurnResult(
        guard_unsaved_file_claim(_strip_footer(out), folder=suggested_folder(server, project)), iid, run_source, _turn_reasoning(),
        resolved_target.get("model", ""), resolved_target.get("tier", ""),
        cache_state.get("status", ""),
    )
    return result if return_result else result.content


@_capture_http_provider_request
def _run_structured_prompt(
    prompt, history, tier, schema, context_size="", metrics=None,
    session="", capture_request_id="", capture_turn_id="", capture_stream=False,
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


_STRUCTURED_SCHEMA_KEYS = _chat_helpers.STRUCTURED_SCHEMA_KEYS
_STRUCTURED_TYPES = _chat_helpers.STRUCTURED_TYPES
_STRUCTURED_SCHEMA_MAX_DEPTH = _chat_helpers.STRUCTURED_SCHEMA_MAX_DEPTH
_STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS = _chat_helpers.STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS


def _structured_unique_items_max_items():
    """Read the shared verifier bound after the runtime has been injected."""
    return server._STRUCTURED_UNIQUE_ITEMS_MAX_ITEMS


def _response_format_error(message):
    return HTTPRequestError(400, message)


def _response_format_schema(value):
    return _chat_helpers.response_format_schema(
        value, unique_items_max_fn=_structured_unique_items_max_items,
    )


def _validate_structured_schema(schema, depth=0):
    _chat_helpers.validate_structured_schema(
        schema, depth, unique_items_max_fn=_structured_unique_items_max_items,
    )


_receipt_text = _chat_helpers.receipt_text


def _chat_completion_object(
    content, model="sonder", iid=None, reasoning="", elapsed_ms=None,
    receipt=None, activity_response=None,
):
    return _chat_helpers.chat_completion_object(
        content, model=model, iid=iid, reasoning=reasoning, elapsed_ms=elapsed_ms,
        receipt=receipt, activity_response=activity_response,
        runtime=server, chat_usage_fn=chat_formatting.chat_usage,
    )


_chunk = _chat_helpers.format_sse_chunk


def _served_command_visible(command, context=None):
    return _slash_helpers.served_command_visible(
        _SERVE_MOD, command, context=context,
        unbound_sentinel=tool_contract.SYSTEM_OPERATION_UNBOUND,
    )


def _served_commands(context=None):
    return _slash_helpers.served_commands(_SERVE_MOD, context=context)


def _served_http_commands(context=None):
    return _slash_helpers.served_http_commands(_SERVE_MOD, context=context)


def _served_help_text(topic, context=None):
    return _slash_helpers.served_help_text(_SERVE_MOD, topic, context=context)


def _served_tool_manifest(context):
    return _slash_helpers.served_tool_manifest(_SERVE_MOD, context)


def _served_tool_capability_manifest(context):
    return _slash_helpers.served_tool_capability_manifest(_SERVE_MOD, context)


def _commands_index_payload(context=None):
    return _slash_helpers.commands_index_payload(_SERVE_MOD, context=context)


def _commands_complete_payload(query, limit="", context=None):
    return _slash_helpers.commands_complete_payload(
        _SERVE_MOD, query, limit=limit, context=context,
        max_limit=COMPLETE_MAX_LIMIT, completion_limit_fn=_completion_limit,
    )


def _commands_help_payload(topic="", context=None):
    return _slash_helpers.commands_help_payload(_SERVE_MOD, topic=topic, context=context)


class ServeHTTPServer(BoundedConnectionsMixin, ThreadingHTTPServer):
    """The served listener, with a TCP backlog sized for request bursts.

    ``socketserver`` listens with a backlog of 5.  A burst of concurrent
    clients then overflows the kernel accept queue and some connections are
    reset before the admission layer can queue them or answer 429, so the
    backlog must at least cover the default admission capacity.  Connection
    threads are capped (see ``connection_limit``) so slow or idle clients
    cannot grow them without bound.
    """

    request_queue_size = 128
    max_connections = max(16, min(4096, _env_int("SONDER_HTTP_MAX_CONNECTIONS", 256)))


_BODY_GUARDED_INVENTORY_ROUTES = _request_security.BODY_GUARDED_INVENTORY_ROUTES


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
        _request_security.linger_before_close(self, _SERVE_MOD)

    def handle(self):
        """Treat a client reset during HTTP framing as a normal disconnect."""
        try:
            super().handle()
        except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True

    def _observatory_origin_request(self, origin):
        """True for an Observatory-only origin on a telemetry GET/OPTIONS route."""
        return (
            origin is not None
            and origin in OBSERVATORY_ORIGINS
            and getattr(self, "command", "") in ("GET", "OPTIONS")
            and _request_route(getattr(self, "path", "")) in _TELEMETRY_ROUTES
        )

    def _cors(self):
        _request_security.send_cors_headers(self, _SERVE_MOD)

    def send_error(self, code, message=None, explain=None):
        if is_app_control_route(getattr(self, "path", "")) and hasattr(self, "headers"):
            self._app_control_request = True
            self._send_json_payload({"ok": False, "error": {"code": "APP_CONTROL_REQUEST_REFUSED"}},
                status=code, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
            return
        super().send_error(code, message, explain)

    def log_message(self, fmt, *args):
        if (is_artifact_route(getattr(self, "path", ""))
                or is_memory_replication_route(getattr(self, "path", ""))
                or is_app_control_route(getattr(self, "path", ""))):
            sys.stderr.write("[sonder_serve] private transfer response\n")
            return
        sys.stderr.write("[sonder_serve] %s\n" % (fmt % args))

    def do_OPTIONS(self):
        # BaseHTTPRequestHandler may reuse this Handler for several HTTP/1.1
        # requests. A correlation ID is a request receipt, never a socket
        # receipt, so discard the prior request's cached value first.
        self._correlation_id = ""
        # Nor may the prior request's principal declaration survive it.
        _bind_request_build_principal(None)
        self._operation_context = None
        self._request_started = time.monotonic()
        self._request_body_consumed = False
        self._app_control_request = False
        self._memory_replication_request = False
        self._spanda_headers = None
        if self._reject_disallowed_host():
            return
        if handle_memory_replication(
                self, "OPTIONS", _MEMORY_REPLICATION_RECEIVER):
            return
        if handle_app_control(self, "OPTIONS", _APP_CONTROL_BINDING,
                deployment_authorized=_app_control_deployment_authorized,
                work_binding=_app_managed_work_binding):
            return
        if handle_artifact_transfer(self, "OPTIONS", _ARTIFACT_TRANSFER_BINDING, max_request_bytes=MAX_REQUEST_BYTES):
            return
        if self._reject_disallowed_origin():
            return
        must_close = self._close_for_unread_body()
        self.send_response(204)
        self._cors()
        if must_close:
            self.send_header("Connection", "close")
        self.send_header("X-Sonder-Elapsed-Ms", "0")
        self.end_headers()

    def _listener_port(self):
        server_address = getattr(getattr(self, "server", None), "server_address", None)
        if isinstance(server_address, tuple) and len(server_address) >= 2:
            port = server_address[1]
            if type(port) is int and port > 0:
                return port
        return BOUND_PORT or CONFIGURED_PORT

    def _reject_disallowed_host(self):
        return _request_security.reject_disallowed_host(self, _SERVE_MOD)

    def _reject_disallowed_origin(self):
        headers = getattr(self, "headers", None)
        if headers is None:
            return False
        origins = headers.get_all("Origin") if hasattr(headers, "get_all") else None
        if origins is not None and len(origins) > 1:
            _serve_logger.debug("_reject_disallowed_origin: rejecting duplicate Origin headers")
            if (
                self.command == "POST"
                and _request_route(self.path) == "/v1/chat/completions"
            ):
                self._record_chat_completion_metric(
                    sonder_lifecycle.get(), "cors_rejected",
                    getattr(self, "_request_started", time.monotonic()),
                )
            self._send_json_payload(
                {"error": {"message": "origin is not allowed", "type": "cors",
                           "code": "forbidden_origin"}},
                status=403,
            )
            return True
        origin = headers.get("Origin")
        if origin is None or origin in CORS_ORIGINS:
            return False
        if _WEB_APP.enabled and getattr(self, "_host_trusted", False) and web_app.same_origin_request(self.headers, https_declared=TLS_TERMINATED_BY_PROXY):
            return False
        if self._observatory_origin_request(origin):
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
            {"error": {"message": "origin is not allowed", "type": "cors",
                       "code": "forbidden_origin"}},
            status=403,
        )
        return True

    def _request_auth_context(self):
        return _request_security.bind_request_auth_context(
            self, _SERVE_MOD,
            role_tool_policy=role_tool_policy,
            permission_policy=permission_policy,
        )

    def _peer(self):
        return self.client_address[0] if self.client_address else ""

    def _client_ip(self):
        """Resolve the client IP; X-Forwarded-For only via a declared proxy.

        A loopback peer is not by itself a proxy: without the operator's
        ``tls_terminated_by_proxy`` declaration any local process could rotate
        the header to dodge the authentication-failure limiter, or to spend
        another address's budget.
        """
        return forwarded_client_ip(
            self._peer(), self.headers.get("X-Forwarded-For", ""),
            proxy_declared=TLS_TERMINATED_BY_PROXY,
            trusted_networks=_TRUSTED_PROXY_NETWORKS,
        )

    def _correlation(self):
        if not getattr(self, "_correlation_id", ""):
            self._correlation_id = sonder_lifecycle.new_correlation_id()
        return self._correlation_id

    def _record_auth_failure(self, reason="invalid-credentials"):
        sonder_lifecycle.get().record_auth_failure(self._client_ip(), reason)

    def _send_auth_error(self, reason="invalid-credentials"):
        _serve_logger.debug(f"_send_auth_error: peer={self._peer()!r}, reason={reason!r}")
        self._record_auth_failure(reason)
        self._send_json_payload({
            "error": {"message": "authentication required", "type": "auth",
                      "code": "UNAUTHENTICATED",
                      "correlation_id": self._correlation()},
        }, status=401)

    def _auth_rate_limited(self):
        """Token-bucket authentication-failure limiter (admission step 3)."""
        return self._auth_throttle_gate(reserve=False)

    def _auth_reserve_refused(self):
        """Credential-checking routes (``login_throttle_http``): spend a token
        in the same locked step as the check; the route refunds it on success.
        """
        return self._auth_throttle_gate(reserve=True)

    def _auth_throttle_gate(self, *, reserve):
        client = self._client_ip()
        lifecycle = sonder_lifecycle.get()
        if (lifecycle.reserve_auth_attempt(client) if reserve
                else lifecycle.auth_attempt_allowed(client)):
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
        login_throttle_http.send_throttled(self)
        return True

    def _peer_is_loopback(self):
        # Only with a trusted Host (``_reject_disallowed_host``), and never behind a declared
        # proxy, whose loopback peer relays remote clients (as ``_local_log_dashboard_allowed``).
        return not TLS_TERMINATED_BY_PROXY and getattr(self, "_host_trusted", True) and _is_loopback_host(self._peer())

    def _handle_lifecycle_get(self, path):
        return _read_routes.handle_lifecycle_get(
            self, path,
            lifecycle_get=sonder_lifecycle.get,
            health_facade=_HEALTH_STATUS_FACADE,
            logger=_serve_logger,
        )

    def _handle_admin_drain(self):
        _account_routes.handle_admin_drain(
            self, _SERVE_MOD,
            lifecycle_get=sonder_lifecycle.get,
            error_envelope_fn=sonder_lifecycle.error_envelope,
            thread_factory=owned_runtime_thread,
        )

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
        return _request_security.unread_request_body_bytes(self)

    def _settle_unread_request_body(self):
        return _request_security.settle_unread_request_body(self, _SERVE_MOD)

    def _close_for_unread_body(self):
        must_close = self._settle_unread_request_body()
        if must_close:
            self.close_connection = True
        return must_close

    def _send_json_payload(self, payload, status=200, headers=None, elapsed_ms=None):
        return _request_security.send_json_payload(
            self, _SERVE_MOD, payload, status=status, headers=headers, elapsed_ms=elapsed_ms,
        )

    def _send_binary_payload(self, body, *, content_type, digest, status=200, headers=None):
        return _request_security.send_binary_payload(
            self, body, content_type=content_type, digest=digest, status=status, headers=headers,
        )

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
        # The live Observatory turn ends with the response; remember why.
        self._telemetry_result = result

    # -- Observatory live telemetry ------------------------------------------

    def send_response(self, code, message=None):
        # The terminal request.* event reports the status actually written.
        self._last_response_status = code
        super().send_response(code, message)

    def _begin_chat_turn_telemetry(self, *, model, stream, session_id):
        observatory_routes.begin_chat_turn(
            self, application=_live_telemetry_application(),
            bind_operation_context=bind_operation_context, log=_serve_logger,
            model=model, stream=stream, session_id=session_id,
        )

    def _finish_chat_turn_telemetry(self):
        observatory_routes.finish_chat_turn(
            self, log=_serve_logger, application_for=_live_telemetry_application)

    def _handle_telemetry_get(self, path):
        observatory_routes.serve_get(
            self, path,
            loopback_listener=not TLS_TERMINATED_BY_PROXY and _is_loopback_host(HOST),
            admin_authorized=_admin_authorized,
            application_for=lambda: _live_telemetry_application(build=True),
            ecosystem_for=_ecosystem_document,
            auth_required=lambda: _effective_auth_mode() != "local-open",
            stream=lambda feed: observatory_routes.stream_telemetry(
                self, feed,
                query=urllib.parse.parse_qs(
                    urllib.parse.urlsplit(self.path).query, keep_blank_values=True,
                ),
                lifecycle=sonder_lifecycle.get(), idle_timeout=STREAM_IDLE_TIMEOUT_SECONDS,
                peer_closed=_socket_peer_closed,
            ),
        )

    def _read_json(self, *, max_bytes=None):
        return _request_security.read_json(self, _SERVE_MOD, max_bytes=max_bytes)

    def _validate_request_framing(self):
        _request_security.validate_request_framing(self)

    def _validate_idempotency_key(self):
        _request_security.validate_idempotency_key(self)

    def _handle_build_request(self, method, path, payload=None):
        """``/v1/build/*``: C++ build tools through the typed gateway.

        Developer authority is required (these routes run host builds). The
        call runs as the authenticated principal with ``source="http"``, so the
        permission modes grade it unattended: under ``manual`` a build is
        refused with the standard remedies. See
        ``interfaces/http/facades/build_tools.py``.
        """
        if not isinstance(path, str) or not path.startswith("/v1/build/"):
            return False
        from sonder_runtime.interfaces.http.facades.build_tools import BuildHttpRoutes

        return self._dispatch_developer_gateway_route(
            method, path, payload, BuildHttpRoutes, invalid_code="INVALID_BUILD_REQUEST",
            read_noun="build reads", admin_only=False,
        )

    def _handle_test_tools_request(self, method, path, payload=None):
        """``/v1/tools/test-run`` and ``/v1/tools/output-digest``.

        Admin authority is required, as for ``/v1/tools/crash-*``: a test run
        executes the project's own code, and a digest reads any log-like file
        under the file roots. An admin caller also carries the configured
        workspace roots, so the planner's per-caller grant check applies.
        Then one typed gateway call as the authenticated principal with
        ``source="http"`` (graded unattended; ``test_run`` is execution). See
        ``interfaces/http/facades/testing_tools.py``.
        """
        from sonder_runtime.interfaces.http.facades.testing_tools import (
            TestRunHttpRoutes,
            owns,
        )

        if not owns(path):
            return False
        return self._dispatch_developer_gateway_route(
            method, path, payload, TestRunHttpRoutes, invalid_code="INVALID_TEST_REQUEST",
            read_noun="test run reads", admin_only=True,
        )

    def _dispatch_developer_gateway_route(self, method, path, payload, routes_type, *,
                                          invalid_code, read_noun, admin_only):
        """Authenticate, bind the principal, and send one typed gateway route.

        ``admin_only`` requires admin authority; otherwise developer or admin
        authority is required (see ``facades/typed_gateway.py``).
        """
        from sonder_runtime.bootstrap.app import default_app
        from sonder_runtime.interfaces.http.facades.typed_gateway import serve_developer_route

        return serve_developer_route(
            self, method, path, payload, routes_type, invalid_code=invalid_code,
            read_noun=read_noun, admin_only=admin_only, admin_authorized=_admin_authorized,
            developer_authorized=_developer_authorized, principal_of=_http_principal,
            workspace_roots_of=_http_workspace_roots, application=default_app,
            query_of=lambda: urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query, keep_blank_values=True,
                max_num_fields=16),
        )

    def _handle_agent_lane_request(self, method, path, payload=None):
        if (
            path != "/v1/background-work"
            and path != "/v1/background-work/clear"
            and path != "/v1/agent-lanes"
            and not path.startswith("/v1/agent-lanes/")
        ):
            return False
        from sonder_runtime.bootstrap.app import default_app
        from sonder_runtime.interfaces.http.agent_work_routes import handle_agent_work_request
        from sonder_runtime.interfaces.http.background_work import runtime_background_work
        from sonder_runtime.adapters.persistence import autopilot_store, fleet_store
        from sonder_runtime.adapters.persistence.background_work import fleet_snapshot, autopilot_snapshot

        def _clear_background(auth, *, project=""):
            owner = _task_account_scope(auth)
            cleared_autopilot = autopilot_store.clear_finished(request_owner=owner)
            cleared_fleets = (
                fleet_store.clear_finished(project=project)
                if _admin_authorized(auth)
                else 0
            )
            return {
                "cleared_autopilot": cleared_autopilot,
                "cleared_fleets": cleared_fleets,
            }

        return handle_agent_work_request(
            self, method, path, payload, application_of=default_app,
            context_of=_http_debug_context, policy=permission_policy, logger=_serve_logger,
            allow_creation_of=_admin_authorized,
            clear_background_of=_clear_background,
            query_of=lambda handler: urllib.parse.parse_qs(
                urllib.parse.urlsplit(handler.path).query, keep_blank_values=True, max_num_fields=16),
            state_home_of=lambda application: getattr(
                getattr(getattr(application, "config", None), "state", None), "home", "") or runtime_paths.default_home(),
            background_of=lambda application, auth: runtime_background_work(
                application, auth, fleet_snapshot=fleet_snapshot, autopilot_snapshot=autopilot_snapshot,
                admin_authorized=_admin_authorized, request_owner=_task_account_scope),
        )

    def do_PUT(self):
        with contextlib.ExitStack() as stack:
            self._turn_stack = stack
            self._mode_scope_bound = False
            try:
                self._handle_put_request()
            finally:
                self._turn_stack = None
                self._mode_scope_bound = False

    def _handle_put_request(self):
        self._correlation_id = ""
        _bind_request_build_principal(None)
        self._request_started = time.monotonic()
        self._request_body_consumed = False
        self._app_control_request = False
        self._memory_replication_request = False
        self._spanda_headers = None
        if self._reject_disallowed_host():
            return
        if handle_memory_replication(
                self, "PUT", _MEMORY_REPLICATION_RECEIVER):
            return
        if handle_app_control(self, "PUT", _APP_CONTROL_BINDING,
                deployment_authorized=_app_control_deployment_authorized,
                work_binding=_app_managed_work_binding):
            return
        if not handle_artifact_transfer(self, "PUT", _ARTIFACT_TRANSFER_BINDING, max_request_bytes=MAX_REQUEST_BYTES):
            self._send_not_found()

    def _handle_ollama_pool_admin(self, method):
        _read_routes.handle_ollama_pool_admin(
            self, _SERVE_MOD, method,
            parse_query=lambda raw_path, max_fields: urllib.parse.parse_qs(
                urllib.parse.urlsplit(raw_path).query,
                keep_blank_values=True, max_num_fields=max_fields,
            ),
        )

    def do_GET(self):
        with contextlib.ExitStack() as stack:
            self._turn_stack = stack
            self._mode_scope_bound = False
            try:
                self._handle_get_request()
            finally:
                self._turn_stack = None
                self._mode_scope_bound = False

    def _handle_get_request(self):
        self._correlation_id = ""
        _bind_request_build_principal(None)
        self._request_started = time.monotonic()
        self._request_body_consumed = False
        self._app_control_request = False
        self._memory_replication_request = False
        self._spanda_headers = None
        if self._reject_disallowed_host():
            return
        if handle_memory_replication(self, "GET", _MEMORY_REPLICATION_RECEIVER):
            return
        if handle_app_control(
            self, "GET", _APP_CONTROL_BINDING,
            deployment_authorized=_app_control_deployment_authorized,
            work_binding=_app_managed_work_binding,
        ):
            return
        if handle_artifact_transfer(self, "GET", _ARTIFACT_TRANSFER_BINDING, max_request_bytes=MAX_REQUEST_BYTES):
            return
        if web_app.handle_web_app(self, _WEB_APP):
            return
        if self._reject_disallowed_origin():
            return
        path = _request_route(self.path)
        if path == "/v1/sonder/ollama-pool":
            self._handle_ollama_pool_admin("GET")
            return
        _serve_logger.debug(f"do_GET: path={path!r}, peer={self._peer()!r}")
        log_page_allowed = (
            getattr(self, "_host_trusted", True)
            and _local_log_dashboard_allowed(self._peer())
        )
        if path == "/" and log_page_allowed:
            self._send_local_log_page()
            return
        if path == "/v1/local/server-log":
            if not log_page_allowed:
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
            port = int(getattr(self.server, "server_port", self.server.server_address[1]))
            self._send_json_payload(
                sonder_health.response_payload(
                    LAUNCHER_HEALTH_TOKEN, nonce, port, role=RUNTIME_ROLE,
                )
            )
            return
        if self._auth_rate_limited():
            return
        if _read_routes.handle_generated_model_get(
            self, path, model3d_generation=_model3d_generation,
            parse_query=lambda raw_path: urllib.parse.parse_qs(urllib.parse.urlsplit(raw_path).query),
        ):
            return
        if self._handle_agent_lane_request("GET", path):
            return
        if self._handle_build_request("GET", path):
            return
        if self._handle_test_tools_request("GET", path):
            return
        if path == "/v1/compute/nodes":
            self._with_compute_inventory_admission(self._handle_compute_inventory_read)
            return
        if path == "/v1/tools/inventory":
            self._with_tool_inventory_admission(self._handle_tool_inventory_read)
            return
        if self._handle_debug_tools_request("GET", path):
            return

        def _default_app():
            from sonder_runtime.bootstrap.app import default_app
            return default_app()

        def _update_status():
            import sonder_runtime.adapters.updates.engine as sonder_update_engine
            return sonder_update_engine.UpdateManager().status()

        _read_routes.dispatch_authenticated_get(
            self, _SERVE_MOD, path,
            default_app_fn=_default_app,
            update_status_fn=_update_status,
            parse_query=lambda raw_path: urllib.parse.parse_qs(
                urllib.parse.urlsplit(raw_path).query, keep_blank_values=True,
            ),
            raw_query_of=lambda raw_path: urllib.parse.urlsplit(raw_path).query,
            error_envelope_fn=sonder_lifecycle.error_envelope,
            lifecycle_get=sonder_lifecycle.get,
            personas_mod=_personas,
            default_home_fn=runtime_paths.default_home,
            build_operational_capabilities_fn=build_operational_capabilities,
            narrated_status_fn=_narrated_status,
            not_found_errors=(NotFound, KeyError),
            invalid_input_errors=(InvalidInput, ValueError, TypeError),
        )

    def _send_local_log_page(self):
        _request_security.send_local_log_page(self)

    def _handle_commands_get(self):
        return _read_routes.handle_commands_get(
            self, _SERVE_MOD,
            split_url=lambda raw_path: (
                urllib.parse.urlsplit(raw_path).path,
                urllib.parse.urlsplit(raw_path).query,
            ),
            parse_qs=lambda raw_query: urllib.parse.parse_qs(raw_query, keep_blank_values=True),
        )

    def _approval_ports(self):
        """This process's approval ledger, authority check and replay guard."""
        from sonder_runtime.interfaces.http.facades.approval_routes import HttpApprovalPorts

        return HttpApprovalPorts(
            developer_authorized=_developer_authorized,
            approve_authority_error=lambda context: _system_operation_authority_error(
                SYSTEM_OPERATION_TOOLS["permission_approve"], context),
            error_envelope=lambda *a, **k: sonder_lifecycle.error_envelope(*a, **k),
            ledger=lambda: permission_policy.approval_ledger(),
            idempotent_action=_idempotent_http_action,
            send_idempotency_refusal=_send_idempotency_refusal,
            approver_of=_http_approver, audit=_audit_http_approval,
            clock=lambda: time.time(), log=_serve_logger,
            unquote=lambda segment: urllib.parse.unquote(segment, errors="strict"),
        )

    def _handle_approvals_get(self):
        """``GET /v1/approvals``: refused calls waiting for approval, and approvals."""
        from sonder_runtime.interfaces.http.facades import approval_routes

        split = urllib.parse.urlsplit(self.path)
        return approval_routes.serve_get(
            self, split.path,
            lambda: urllib.parse.parse_qs(split.query, keep_blank_values=True),
            self._approval_ports(),
        )

    def _handle_approvals_post(self, path, req, context):
        """``POST /v1/approvals/<call_id>`` and ``POST /v1/approvals/revoke/<nonce>``.

        Needs the ``permission_approve`` role (administrator); approving is refused under
        local-open (403 APPROVAL_REQUIRES_AUTHENTICATED_ADMIN). See ``facades/approval_routes.py``.
        """
        from sonder_runtime.interfaces.http.facades import approval_routes

        approval_routes.serve_post(self, path, req, context, self._approval_ports())

    def _handle_client_tools_post(self, path, req, context):
        """``POST /v1/client-tools/<call_id>/result``."""
        _client_tools_http.handle_client_tool_result_post(
            self, path, req, context, broker=_CLIENT_TOOL_BROKER, principal=_state_principal(context),
        )

    def _handle_permission_mode_get(self):
        """Current autonomy mode, so a client can show it before you send."""
        route = urllib.parse.urlsplit(self.path).path.rstrip("/") or "/"
        if route != "/v1/permission-mode":
            return False
        _chat_tool_stream.handle_permission_mode_get(
            self, permission_policy=permission_policy, principal_of=_mode_principal,
            authority_error=_system_operation_authority_error,
            observe_state=_observe_client_control_state, mode_data=server.permission_mode_data,
        )
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
        is_admin = admin_auth.require(account, "admin")[0]
        if context.get("mode") != "local-open" and not is_admin:
            if run.get("request_owner") != _fanout_request_owner(context):
                # Do not disclose another developer's run identifier.
                return None, (404, "fanout run was not found")
        return run, None

    def _handle_work_run_request(self, method, path, context=None):
        """``GET /v1/work-runs[/<id>]`` and ``POST /v1/work-runs/<id>/cancel``."""
        from sonder_runtime.interfaces.http.facades import work_runs
        return work_runs.serve_request(
            self, method, path, context, runner=_WORK_RUNNER,
            developer_authorized=_developer_authorized, principal_of=_state_principal,
            store_errors=(OSError, sqlite3.Error), log=_serve_logger,
            status_projection=lambda row: _enrich_work_record(row, include_detail=_execution_feed_detail_allowed(context or self._request_auth_context())), cancel_projection=_cancel_linked_work,
        )

    def _handle_fanout_get(self):
        from sonder_runtime.interfaces.http.facades import fanout

        return fanout.serve_get(
            self, urllib.parse.urlsplit(self.path).path.rstrip("/"),
            parse_query=lambda: urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query, keep_blank_values=True),
            developer_authorized=_developer_authorized,
            admin_authorized=lambda ctx: admin_auth.require(ctx.get("account") or {}, "admin")[0],
            request_owner_of=_fanout_request_owner, runtime=server,
        )

    def _handle_fanout_post(self, path, req, context):
        """Mutate or locally synthesize one caller-authorized fanout receipt."""
        from sonder_runtime.interfaces.http.facades import fanout

        # Pass the injected compatibility namespace rather than reaching
        # around it: this keeps the route bound to the composition-time
        # runtime and preserves the patch seam for callers that replace a
        # single legacy operation in tests.
        return fanout.serve_post(
            self, path, req, context, runtime=server, account_auth=admin_auth,
            idempotent_http_action=_idempotent_http_action,
            send_idempotency_refusal=_send_idempotency_refusal,
        )

    def _handle_client_reconnect(self, req, context):
        """Plan one client reconnect against the served schema and streams."""
        if not context["authorized"]:
            self._send_auth_error()
            return
        status, body = _client_protocol.reconnect_response(_client_protocol_host(), req)
        self._send_json_payload(body, status=status)

    def _handle_permission_mode_post(self, req, context=None):
        """Switch the autonomy mode. Deliberately cannot grant elevation."""
        _chat_tool_stream.handle_permission_mode_post(
            self, req, context, permission_policy=permission_policy,
            principal_of=_mode_principal, idempotent_action=_idempotent_http_action,
            send_idempotency_refusal=_send_idempotency_refusal,
            observe_state=_observe_client_control_state, mode_data=server.permission_mode_data,
            approver_of=_http_approver, audit_transition=server._http_chat_tools().audit_mode_transition,
            error_envelope=sonder_lifecycle.error_envelope,
            send_escalation_refusal=_mode_post.send_escalation_refusal,
        )


    def _with_compute_inventory_admission(self, handler):
        from sonder_runtime.interfaces.http.facades.compute_inventory import inventory_request_slot
        with inventory_request_slot() as admitted:
            if not admitted:
                self._send_json_payload({"error": {"code": "COMPUTE_INVENTORY_BUSY"}}, status=429)
                return
            handler()

    def _handle_compute_inventory_read(self):
        from sonder_runtime.bootstrap.app import default_app

        _read_routes.handle_compute_inventory_read(
            self, _SERVE_MOD, default_app_fn=default_app,
            parse_query=lambda raw_path, max_fields: urllib.parse.parse_qs(
                urllib.parse.urlsplit(raw_path).query,
                keep_blank_values=True, max_num_fields=max_fields,
            ),
        )

    def _handle_compute_inventory_refresh(self):
        if _request_route(self.path) != "/v1/compute/nodes/refresh":
            return False
        self._with_compute_inventory_admission(self._handle_admitted_compute_refresh)
        return True

    def _handle_admitted_compute_refresh(self):
        from sonder_runtime.bootstrap.app import default_app

        return _account_routes.handle_admitted_compute_refresh(
            self, _SERVE_MOD, default_app_fn=default_app,
        )

    def _with_tool_inventory_admission(self, handler):
        from sonder_runtime.interfaces.http.facades.host_tools import tool_inventory_request_slot
        with tool_inventory_request_slot() as admitted:
            if not admitted:
                self._send_json_payload({"error": {"code": "TOOL_INVENTORY_BUSY"}}, status=429)
                return
            handler()

    @staticmethod
    def _tool_inventory_service_factory():
        from sonder_runtime.bootstrap.app import default_app

        def factory():
            services = getattr(default_app(), "developer_tools", None)
            return getattr(services, "inventory", None) if services is not None else None

        return factory

    def _handle_tool_inventory_read(self):
        _read_routes.handle_tool_inventory_read(
            self, _SERVE_MOD, service_factory=self._tool_inventory_service_factory(),
            parse_query=lambda raw_path, max_fields: urllib.parse.parse_qs(
                urllib.parse.urlsplit(raw_path).query,
                keep_blank_values=True, max_num_fields=max_fields,
            ),
        )

    def _handle_tool_inventory_refresh(self):
        if _request_route(self.path) != "/v1/tools/inventory/refresh":
            return False
        self._with_tool_inventory_admission(self._handle_admitted_tool_inventory_refresh)
        return True

    def _handle_admitted_tool_inventory_refresh(self):
        _account_routes.handle_admitted_tool_inventory_refresh(
            self, _SERVE_MOD, service_factory=self._tool_inventory_service_factory(),
        )

    # --- crash / profile digests (admin-only; lane D routes) -------------------

    def _debug_tools_context(self, auth):
        """Typed HTTP context: source=http, principal from the account."""
        return _http_debug_context(auth, self._correlation())

    def _handle_debug_tools_request(self, method, route):
        """``/v1/tools/crash-*``, ``profile-*`` and ``debug-runs/<id>`` (admin)."""
        from sonder_runtime.interfaces.http.facades import debug_tools

        def facade():
            from sonder_runtime.bootstrap.app import default_app

            return _debug_tools_http_facade(default_app())

        return debug_tools.serve_request(
            self, method, route, admin_authorized=_admin_authorized,
            request_error=HTTPRequestError, facade_factory=facade,
            parse_query=lambda: urllib.parse.parse_qs(
                urllib.parse.urlsplit(self.path).query, keep_blank_values=True,
                max_num_fields=1),
        )

    def _handle_account_logout(self):
        _account_routes.handle_account_logout(
            self, _SERVE_MOD,
            previous_key_valid=sonder_secrets.previous_key_valid,
            lifecycle_get=sonder_lifecycle.get,
        )

    @_deferred_post
    def do_POST(self):
        # One turn scope per request: the ambient OperationContext and the
        # Observatory turn are bound inside it and always unwound, and a
        # started turn always gets exactly one terminal request.* event.
        observatory_routes.reset_chat_turn(self)
        with contextlib.ExitStack() as stack:
            self._turn_stack = stack
            self._mode_scope_bound = False
            try:
                self._handle_post_request()
            finally:
                self._turn_stack = None
                self._mode_scope_bound = False
                self._finish_chat_turn_telemetry()

    def _handle_post_request(self):
        # Keep-alive reuses Handler instances; see do_OPTIONS for why this is
        # reset before every externally visible request.
        self._correlation_id = ""
        _bind_request_build_principal(None)
        self._request_started = time.monotonic()
        # BaseHTTPRequestHandler reuses this instance for HTTP/1.1 keep-alive
        # requests. The terminal-metric latch is per request, never per socket,
        # and so is the record of whether this request's body was read.
        self._chat_completion_metrics_recorded = False
        self._request_body_consumed = False
        self._app_control_request = False
        self._memory_replication_request = False
        self._spanda_headers = None
        self._early_stream = None
        if self._reject_disallowed_host():
            return
        if handle_memory_replication(
                self, "POST", _MEMORY_REPLICATION_RECEIVER):
            return
        if handle_app_control(self, "POST", _APP_CONTROL_BINDING,
                deployment_authorized=_app_control_deployment_authorized,
                work_binding=_app_managed_work_binding, accounts=admin_auth,
                swarm_status=lambda: server.master_orchestrator.get_swarm_status()):
            return
        if handle_artifact_transfer(self, "POST", _ARTIFACT_TRANSFER_BINDING, max_request_bytes=MAX_REQUEST_BYTES):
            return
        if self._handle_compute_inventory_refresh():
            return
        if self._handle_tool_inventory_refresh():
            return
        if self._handle_debug_tools_request("POST", _request_route(self.path)):
            return
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
        if _request_route(self.path) == "/v1/sonder/ollama-pool":
            self._handle_ollama_pool_admin("POST")
            return
        _maybe_live_reload()
        path = _request_route(self.path)
        self._correlation()
        try:
            self._validate_request_framing()
            self._validate_idempotency_key()
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
        if path == '/v1/sonder/logout':
            if not _ACCOUNT_LOGOUT_ADMISSION.acquire(blocking=False):
                self._send_json_payload(
                    {'ok': False, 'message': 'session revocation busy'},
                    status=429, headers={'Retry-After': '1'},
                )
                return
            try:
                self._handle_account_logout()
            finally:
                _ACCOUNT_LOGOUT_ADMISSION.release()
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
        if self._handle_agent_lane_request("POST", path, req):
            return
        if self._handle_build_request("POST", path, req):
            return
        if self._handle_test_tools_request("POST", path, req):
            return
        if path == _CLIENT_RECONNECT_ROUTE:
            self._handle_client_reconnect(req, context)
            return
        def _default_app():
            from sonder_runtime.bootstrap.app import default_app
            return default_app()

        if _account_routes.dispatch_post_control_routes(
            self, _SERVE_MOD, path, req, context,
            default_app_fn=_default_app,
            error_envelope_fn=sonder_lifecycle.error_envelope,
            conflict_errors=(Conflict,),
            not_found_errors=(NotFound,),
            invalid_input_errors=(InvalidInput, ValueError, TypeError),
        ):
            return
        handled_model, req, model_operation, request_temperature, facade = (
            _account_routes.normalize_model_post_route(
                self, _SERVE_MOD, path, req, context,
                record_early_chat_metric=record_early_chat_metric,
                lifecycle_get=sonder_lifecycle.get,
                error_envelope_fn=sonder_lifecycle.error_envelope,
                invalid_input_cls=InvalidInput,
            )
        )
        if handled_model:
            return
        if model_operation == "responses":
            path = "/v1/chat/completions"
        from ...domain.cloud_access import has_legacy_error_prefix

        if _account_routes.dispatch_post_account_and_policy_routes(
            self, _SERVE_MOD, path, req, context,
            error_envelope_fn=sonder_lifecycle.error_envelope,
            login_throttle_http=login_throttle_http,
            has_legacy_error_prefix_fn=has_legacy_error_prefix,
            thin_routes_serve=thin_routes.serve,
        ):
            return
        if path != "/v1/chat/completions":
            _serve_logger.debug(f"do_POST: unrecognized path={path!r}, returning 404")
            self._send_json_payload(
                {"error": {"message": "not found", "type": "not_found"}}, status=404
            )
            return

        def _load_spanda_http():
            import sonder_runtime.platform.spanda_http as spanda_http
            return spanda_http

        _chat_routes.handle_chat_completions(
            self,
            _SERVE_MOD,
            req,
            context,
            model_operation=model_operation,
            request_temperature=request_temperature,
            facade=facade,
            record_early_chat_metric=record_early_chat_metric,
            deps=_chat_routes.ChatTurnDeps(
                lifecycle_mod=sonder_lifecycle,
                context_policy_parse_strict=context_policy.parse_strict,
                chat_temperature_mod=_chat_temperature,
                chat_effort_mod=_chat_effort,
                personas_mod=_personas,
                image_generation_mod=_image_generation,
                model3d_generation_mod=_model3d_generation,
                thinking_controls_mod=_thinking_controls,
                reference_lookup_lanes_mod=_reference_lookup_lanes,
                chat_formatting_mod=chat_formatting,
                load_spanda_http=_load_spanda_http,
                acquire_chat_slot=lambda lifecycle_obj: lifecycle_obj.acquire_request_slot(mutating=True, owner=_admission_request_owner(context)),
                run_natural_fanout=lambda natural_model: server._model_fanout_authorized(
                    natural_model["prompt"],
                    scope=natural_model["scope"],
                    profile=natural_model.get("profile", ""),
                    request_owner=_fanout_request_owner(context),
                    request_role=_fanout_request_role(context),
                ),
                run_natural_ensemble=lambda natural_model, storage_project: server.ensemble_answer(
                    natural_model["prompt"],
                    tiers=natural_model["tiers"],
                    project=storage_project,
                    require_all_tiers=True,
                ),
            ),
        )

    def _send_error_completion(self, text, stream):
        if stream:
            self._send_stream(text, "sonder")
        else:
            self._send_json(_chat_completion_object(text, "sonder"))

    def _send_json(self, obj, elapsed_ms=None):
        self._send_json_payload(obj, elapsed_ms=elapsed_ms)

    def _send_stream(self, content, model, iid=None, elapsed_ms=None, receipt=None,
                     usage=None, activity_response=None):
        return _chat_routes.send_stream(
            self, _SERVE_MOD, content, model, iid=iid, elapsed_ms=elapsed_ms,
            receipt=receipt, usage=usage, activity_response=activity_response,
        )

    def _write_stream_body(self, iid, model, content, elapsed_ms, receipt, usage,
                           activity, lock=None):
        # Live deltas already sent (early SSE) are reconciled with the answer.
        return _live_stream.write_stream_body(
            self, _chunk, iid, model, content, elapsed_ms, receipt, usage, activity, lock=lock,
        )

    def _run_streamable_prompt(self, *args, tool_turn=None, **kwargs):
        """The plain model turn; a streaming provider forwards live deltas."""
        from sonder_runtime.application.chat import tool_loop as _tool_loop
        with _tool_loop.bind_turn(tool_turn), _live_stream.armed_for(self, _chunk, hold_code=server._code_gate_enabled()), _live_stream.stream_admission(self, server._bound_request_timeout(None, server.TIMEOUT), bool(kwargs.get("capture_stream")), admission=server._stream_turn_admission):
            return _run_prompt(*args, **kwargs)

    def _begin_early_stream(self, model="sonder"):
        return _chat_routes.begin_early_stream(
            self, _SERVE_MOD, model=model, thread_factory=owned_runtime_thread,
        )

    def _pause_early_stream(self):
        early = getattr(self, "_early_stream", None)
        if early is not None:
            early.stop()
            server.ollama_pool.clear_queue_wait()

    def _end_early_stream_with_error(self, message, error_type, code, model):
        return _chat_routes.end_early_stream_with_error(
            self, _SERVE_MOD, message, error_type, code, model,
        )

    def _send_stream_terminal_error(self, iid, model, *, message=None,
                                    error_type="server_error", code="STREAM_INTERRUPTED"):
        return _chat_routes.send_stream_terminal_error(
            self, iid, model, message=message, error_type=error_type, code=code,
        )


def main(
    config=None,
    *,
    _server_factory=None,
    _close_default_resources=True,
    _after_configure=None,
):
    _serve_logger.info("HTTP server starting")
    _serve_logger.debug("main: starting HTTP server")
    # This process serves several principals: an agent turn that inherited no
    # request declaration shows no build line rather than the owner's.
    from sonder_runtime.bootstrap.build_tools import require_declared_build_brief_principal

    require_declared_build_brief_principal()
    global CONFIGURED_PORT, BOUND_PORT
    global _ARTIFACT_TRANSFER_BINDING, _ARTIFACT_TRANSFER_CONFIG
    global _APP_CONTROL_BINDING, _APP_CONTROL_CONFIG
    global _MEMORY_REPLICATION_SERVICE, _MEMORY_REPLICATION_RECEIVER
    global _SESSION_FACADE, _CONTROL_PLANE_SERVICE
    if _after_configure is not None and not callable(_after_configure):
        raise TypeError("HTTP post-configure hook must be callable or None")
    application = None
    graph_memory = None
    memory_service = None
    memory_receiver = None
    session_facade = None
    control_plane_service = None
    artifact_binding = None
    app_control_binding = None
    thin_handlers = _THIN_HANDLERS
    lifecycle = None
    httpd = None
    port = None
    startup_completed = False
    cloud_refresh_stop = threading.Event()  # managed children join owned threads at stop
    cleanup_errors = []

    def cleanup(name, callback):
        try:
            if callback() is False:
                cleanup_errors.append(name)
                _serve_logger.error("HTTP cleanup was incomplete: %s", name)
        except BaseException:
            cleanup_errors.append(name)
            _serve_logger.error("HTTP cleanup failed: %s", name, exc_info=True)

    # The HTTP adapter publishes several process-global route bindings.  Start
    # one ownership guard before publishing any of them, not only after the
    # listener enters serve_forever, so startup and bind errors cannot leave a
    # live artifact/memory/control route behind.
    try:
        if config is not None:
            previous_artifact = _ARTIFACT_TRANSFER_BINDING
            previous_control = _APP_CONTROL_BINDING
            try:
                configure_typed_config(config)
            finally:
                # A late configure failure may have published a candidate.
                # Capture only a newly selected binding; never take a prior
                # host's resource merely because validation rejected ours.
                if _ARTIFACT_TRANSFER_BINDING is not previous_artifact:
                    artifact_binding = _ARTIFACT_TRANSFER_BINDING
                if _APP_CONTROL_BINDING is not previous_control:
                    app_control_binding = _APP_CONTROL_BINDING
        else:
            artifact_binding = _ARTIFACT_TRANSFER_BINDING
            app_control_binding = _APP_CONTROL_BINDING

        if _SESSION_FACADE is None:
            from sonder_runtime.bootstrap.app import default_app

            application = default_app(config=config)
            configure_session_facade(application.session_http_facade())
            session_facade = _SESSION_FACADE
        if _CONTROL_PLANE_SERVICE is None:
            from sonder_runtime.bootstrap.app import default_app

            if application is None:
                application = default_app(config=config)
            configure_control_plane_service(application.control_plane_snapshot_service)
            control_plane_service = _CONTROL_PLANE_SERVICE
        if application is None and config is not None:
            from sonder_runtime.bootstrap.app import default_app

            application = default_app(config=config)
        if application is not None:
            graph_memory = getattr(application, "memory_replication", None)
            # The route starts and exposes the graph service, but replacement
            # must not close any graph that an outer host still owns.
            configure_memory_replication_service(
                graph_memory, close_replaced=False,
            )
            memory_service = graph_memory
            memory_receiver = _MEMORY_REPLICATION_RECEIVER
        else:
            # A compatibility host with no owned Application has no authority
            # to retain a receiver from a prior typed host selection.
            _detach_memory_replication_route()
        if _after_configure is not None:
            # Managed hosts that need the typed app-control binding can finish
            # their exact application handoff here.  This remains inside the
            # same outer cleanup guard as typed HTTP publication and occurs
            # before listener/security admission.
            _after_configure(application)
            thin_handlers = _THIN_HANDLERS
        port = _selected_listener_port(config)
        # Discovery reads the bound-listener value. Keep it synchronized when
        # the direct compatibility entrypoint (which has no typed config)
        # selects a positional argument or SONDER_PORT.
        CONFIGURED_PORT = port

        _validate_bind_security(HOST, tls_terminated_by_proxy=TLS_TERMINATED_BY_PROXY)
        lifecycle = sonder_lifecycle.get()
        try:
            # STARTING -> MIGRATING -> READY; no listener opens on failure.
            # When config is provided the caller (cmd_serve) already ran
            # migrate_all with the configured busy_timeout_ms — skip the
            # lifecycle's unconfigured duplicate.
            _serve_logger.info("Lifecycle startup initiated (STARTING -> MIGRATING -> READY)")
            lifecycle.startup(run_migrations=config is None)
            startup_completed = True
            _serve_logger.info("Lifecycle startup completed")
        except Exception as error:
            _serve_logger.error("lifecycle startup failed before bind", exc_info=True)
            _serve_logger.critical("lifecycle startup failed before bind, server cannot start", exc_info=True)
            print("startup failed before bind: %s" % error, file=sys.stderr)
            raise SystemExit(1)
        lifecycle.begin_ollama_probe()
        # Resolve the machine's own Host names before the first request, so
        # no request ever waits on the bounded FQDN lookup.
        _machine_host_names()
        try:
            factory = ServeHTTPServer if _server_factory is None else _server_factory
            httpd = factory((HOST, port), Handler)
            try:
                httpd.tls_terminated_by_proxy = bool(TLS_TERMINATED_BY_PROXY)
            except Exception:
                pass
        except OSError:
            _serve_logger.critical(f"server cannot bind to {HOST}:{port}, port may already be in use", exc_info=True)
            raise
        # After a drain completes (signal or /v1/admin/drain), stop accepting.
        lifecycle.coordinator.add_flush_hook(
            lambda: owned_runtime_thread(
                target=httpd.shutdown, daemon=True, name="sonder-httpd-shutdown"
            ).start()
        )
        try:
            _WORK_RUNNER.reconcile()
        except Exception:
            _serve_logger.error("HTTP work run reconciliation failed at startup", exc_info=True)

        def _warm_command_catalog():
            # Without this the first slash command builds the catalog (an AST
            # walk of the legacy dispatch chains, ~2-4 s) inside its request.
            # A failed build caches nothing, so the request path still raises.
            try:
                command_catalog.http_slash_tools()
                command_catalog.catalog()
            except Exception:
                _serve_logger.warning("command catalog warm-up failed", exc_info=True)

        owned_runtime_thread(
            target=_warm_command_catalog, daemon=True, name="sonder-catalog-warmup"
        ).start()
        BOUND_PORT = port
        startup_prewarm.start_default_model_prewarm(
            setting=PREWARM_ON_START, prewarm=lambda tier: server.prewarm_model(tier, announce=True),
            enabled=lambda: server.sonder_speculation.prewarm_enabled(), thread_factory=owned_runtime_thread)
        cloud_model_refresh_schedule.start_scheduled_refresh(
            origin=server.BASE, thread_factory=owned_runtime_thread, stop=cloud_refresh_stop)
        url = "http://%s:%d" % (HOST, port)
        _serve_logger.info(f"Server listening on {url}, auth_mode={_effective_auth_mode()!r}")
        rpc_cluster.start_sharded_backend_probe(thread_factory=owned_runtime_thread)
        _warn_if_local_open()
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
        telemetry_app = application or _live_telemetry_application()
        if _register_telemetry_drain(lifecycle.coordinator, telemetry_app):
            print(
                "observatory telemetry: %s%s (admin authorization)"
                % (_runtime_loopback_base_url(), observability_stream.DISCOVERY_ROUTE)
            )
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
    finally:
        _serve_logger.info("HTTP server shutting down")
        cloud_refresh_stop.set()
        if startup_completed and lifecycle is not None:
            coordinator = getattr(lifecycle, "coordinator", None)
            if not getattr(coordinator, "draining", False):
                cleanup("lifecycle-drain", lambda: lifecycle.drain("server stopping"))
        if lifecycle is not None:
            stop_probe = getattr(lifecycle, "stop_probe", None)
            if callable(stop_probe):
                cleanup("ollama-probe", stop_probe)
        if httpd is not None:
            cleanup("listener", httpd.server_close)
        if port is not None and BOUND_PORT == port:
            BOUND_PORT = None
        # Stop exposing every HTTP-owned route before its backing resource is
        # closed.  Exact identity checks avoid clearing a different host that
        # may have selected a replacement after this one was detached.
        if _ARTIFACT_TRANSFER_BINDING is artifact_binding:
            _ARTIFACT_TRANSFER_BINDING = None
            _ARTIFACT_TRANSFER_CONFIG = None
        if _APP_CONTROL_BINDING is app_control_binding:
            _APP_CONTROL_BINDING = None
            _APP_CONTROL_CONFIG = None
        _detach_memory_replication_route(
            service=memory_service, receiver=memory_receiver,
        )
        if _SESSION_FACADE is session_facade:
            _SESSION_FACADE = None
        if _CONTROL_PLANE_SERVICE is control_plane_service:
            _CONTROL_PLANE_SERVICE = None
        _detach_thin_handlers(handlers=thin_handlers)
        if artifact_binding is not None:
            cleanup("artifact-transfer", artifact_binding.close)
        if _close_default_resources:
            from sonder_runtime.bootstrap.app import close_default_runtime_resources

            cleanup(
                "default-application",
                lambda: close_default_runtime_resources(timeout=5),
            )
        if memory_service is not None and memory_service is not graph_memory:
            cleanup("memory-replication", memory_service.close)
        _serve_logger.info("HTTP server stopped")
    if cleanup_errors:
        raise RuntimeError("HTTP server cleanup is incomplete: " + ", ".join(cleanup_errors))


if __name__ == "__main__":
    main()
