"""HTTP server configuration wiring, conversation state, live-session capture, and job route helpers.

Extracted from ``sonder_runtime/interfaces/http/serve.py`` to keep the HTTP
entrypoint concise while preserving every SPEC-3 layer boundary and runtime
monkeypatch seam.  All platform/adapter/bootstrap dependencies arrive via
explicit callables from ``serve.py``.
"""
from __future__ import annotations

import ipaddress
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from sonder_runtime.application.ports.model_gateway import ModelRequest
from ..web_app import WebAppSettings
from .health_status import HealthStatusFacade

MAX_JOB_CANCEL_REASON = 256
MAX_JOB_ID_LENGTH = 128
MAX_JOB_KIND_LENGTH = 64
MAX_JOB_OPERATION_LENGTH = 128
MAX_JOB_IDEMPOTENCY_LENGTH = 256
MAX_JOB_PARENT_LENGTH = 128

JOB_STREAM_DEFAULT_EVENTS = 64
JOB_STREAM_MAX_EVENTS = 256
JOB_STREAM_DEFAULT_BYTES = 16 * 1024
JOB_STREAM_MAX_BYTES = 64 * 1024
JOB_STREAM_MAX_AFTER = (1 << 63) - 1

LOCAL_LOG_TAIL_BYTES = 64 * 1024

LOCAL_LOG_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Sonder local server</title>
<style>
:root{color-scheme:dark}body{margin:0;background:#07121f;color:#d9efff;font:14px ui-monospace,Consolas,monospace}header{padding:16px 22px;background:#0b2b4a;color:#75cfff;font-weight:700;display:flex;gap:12px;align-items:center;flex-wrap:wrap}main{padding:16px 22px}small{color:#b7cad8;font-weight:400}.controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:12px 0}button{background:#123e62;color:#e8f6ff;border:1px solid #2f739f;border-radius:5px;padding:8px 12px;font:inherit;cursor:pointer}button:hover,button:focus-visible{background:#19547e;outline:2px solid #75cfff;outline-offset:2px}label{padding:8px 4px}pre{white-space:pre-wrap;word-break:break-word;background:#050b12;border:1px solid #27658f;padding:14px;min-height:60vh;max-height:72vh;overflow:auto;line-height:1.45}pre:focus-visible{outline:2px solid #75cfff;outline-offset:2px}.offline{color:#ffca80}
</style></head><body>
<header><span>Sonder local server</span><small id="state" role="status" aria-live="polite">Connecting</small></header>
<main><p>Live, read-only server-log tail. This page is available only from loopback.</p>
<div class="controls" aria-label="Log controls"><button id="pause" type="button" aria-pressed="false">Pause updates</button><button id="refresh" type="button">Refresh now</button><button id="copy" type="button">Copy visible log</button><label><input id="follow" type="checkbox" checked> Follow latest output</label></div>
<pre id="log" tabindex="0" aria-label="Redacted server log" aria-busy="true">Loading...</pre></main>
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


class HTTPRequestError(Exception):
    """Structured HTTP validation error raised while decoding/validating requests."""

    def __init__(self, status: int, message: str, error_type: str = "invalid_request") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.error_type = error_type


class DuplicateJsonObjectKey(ValueError):
    """A request JSON object repeated a member name."""


def reject_duplicate_json_object_key(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """Object-pairs hook that fails closed on repeated JSON keys."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonObjectKey("duplicate JSON object key")
        result[key] = value
    return result


def reject_nonfinite_json_number(_value: str) -> Any:
    """Keep the HTTP boundary to standard finite JSON numbers."""
    raise ValueError("non-finite JSON number is not allowed")


class LiveSessionCaptureFailure(RuntimeError):
    """The HTTP turn cannot claim durable recovery evidence."""


def chat_routing_metadata(selector: Any) -> dict[str, str]:
    """Keep the HTTP chat route observable without storing user text or auth state."""
    return {
        "lane": "chat",
        "reason": (
            "explicit model selection"
            if str(selector or "").strip()
            else "ordinary conversation"
        ),
    }


def fail_live_session_request(
    admission: Any,
    session_id: str,
    request_id: str,
    error_code: str,
) -> Any:
    """Append ``model.failed`` for this HTTP turn's own admitted request, if any."""
    capture, pending = admission if admission else (None, None)
    if (
        capture is None
        or pending is None
        or (pending.session_id, pending.request_id) != (session_id, request_id)
    ):
        return None
    try:
        return capture.fail_request(pending, error_code=error_code)
    except Exception as error:
        raise LiveSessionCaptureFailure from error


def capture_live_session_turn(
    *,
    session_id: str,
    prompt: str,
    history: Any,
    model: str,
    content: Any,
    request_id: str,
    turn_id: str,
    stream: bool,
    provider_capture: Any = None,
    selector: Any = None,
    resolved_tier: Any = None,
    default_app_fn: Callable[[], Any],
    is_legacy_error_reply: Callable[[object], bool],
    logger: Any,
) -> Any:
    """Capture only a named, model-backed HTTP turn through the app graph."""
    if not session_id:
        return None
    try:
        if is_legacy_error_reply(content):
            return fail_live_session_request(
                provider_capture, session_id, request_id, "DEPENDENCY_UNAVAILABLE",
            )
        if provider_capture is not None:
            capture, pending = provider_capture
            if pending.session_id != session_id or pending.request_id != request_id:
                raise ValueError("provider admission owner mismatch")
            return capture.complete_request(pending, model_response=content)

        request = ModelRequest(
            prompt=prompt,
            tier=resolved_tier or selector or model or "sonder",
            history=tuple(history),
            options={"stream": bool(stream)},
            routing_metadata=chat_routing_metadata(selector),
        )
        return default_app_fn().session_capture_service().capture_turn(
            session_id,
            turn_id,
            request,
            request_id=request_id,
            user_message=prompt,
            model_response=content,
        )
    except Exception as error:
        logger.error(
            f"live session capture failed for session_id={session_id!r}, request_id={request_id!r}",
            exc_info=True,
        )
        raise LiveSessionCaptureFailure from error


def local_server_log_tail(
    *,
    default_home: Callable[[], Any],
    redact_text: Callable[[str], str],
    tail_bytes: int = LOCAL_LOG_TAIL_BYTES,
) -> str:
    """Return a bounded, redacted tail for the loopback-only diagnostics page."""
    path = Path(default_home()) / "run" / "sonder_serve.log"
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            stream.seek(max(0, size - tail_bytes))
            raw = stream.read(tail_bytes)
    except FileNotFoundError:
        return (
            "(no launcher-managed server log at %s: the server was started "
            "without the launcher, or its output is redirected elsewhere -- "
            "read the log where that output goes, e.g. the service journal)" % path
        )
    except OSError as error:
        return "(server log at %s could not be read: %s)" % (
            path,
            error.strerror or type(error).__name__,
        )
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    if size > len(raw):
        text = "(showing the latest %d KiB)\n%s" % (tail_bytes // 1024, text)
    return str(redact_text(text))


def job_record_payload(record: Any) -> dict[str, Any]:
    """Serialize one durable job record for HTTP responses."""
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


def job_cancel_id(path: str, *, unquote: Callable[[str], str]) -> str | None:
    """Extract and decode the job id from ``/v1/jobs/<id>/cancel``."""
    prefix = "/v1/jobs/"
    suffix = "/cancel"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    encoded = path[len(prefix) : -len(suffix)]
    if not encoded or "/" in encoded:
        return None
    job_id = unquote(encoded)
    return job_id if job_id and "/" not in job_id else None


def compute_job_route(
    path: str, *, unquote: Callable[[str], str],
) -> tuple[str, Any] | None:
    """Return an exact compute-job operation and one decoded identity."""
    if path == "/v1/compute/jobs":
        return "submit", None
    prefix = "/v1/compute/jobs/"
    if not path.startswith(prefix):
        return None
    suffix = path[len(prefix) :]
    idempotency_prefix = "by-idempotency/"
    if suffix.startswith(idempotency_prefix):
        encoded = suffix[len(idempotency_prefix) :]
        if not encoded or "/" in encoded:
            return None
        value = unquote(encoded)
        return ("by_idempotency", value) if value and "/" not in value else None
    artifact_marker = "/artifacts/"
    if artifact_marker in suffix:
        encoded_job, encoded_name = suffix.split(artifact_marker, 1)
        if not encoded_job or not encoded_name or "/" in encoded_job or "/" in encoded_name:
            return None
        job_id = unquote(encoded_job)
        artifact_name = unquote(encoded_name)
        if not job_id or "/" in job_id or not artifact_name:
            return None
        return "artifact", (job_id, artifact_name)
    if suffix.endswith("/cancel"):
        encoded = suffix[: -len("/cancel")]
        if not encoded or "/" in encoded:
            return None
        value = unquote(encoded)
        return ("cancel", value) if value and "/" not in value else None
    if not suffix or "/" in suffix:
        return None
    value = unquote(suffix)
    return ("status", value) if value and "/" not in value else None


def job_start_payload(req: Mapping[str, Any]) -> dict[str, str]:
    """Validate the bounded identity accepted by the durable job command."""
    required = {"job_id", "kind", "operation_id", "idempotency_key"}
    optional = {"parent_job_id", "parent_session_id"}
    if set(req) - required - optional or not required.issubset(req):
        raise ValueError(
            "job start requires exactly the identity fields job_id, kind, "
            "operation_id, and idempotency_key"
        )
    limits = {
        "job_id": MAX_JOB_ID_LENGTH,
        "kind": MAX_JOB_KIND_LENGTH,
        "operation_id": MAX_JOB_OPERATION_LENGTH,
        "idempotency_key": MAX_JOB_IDEMPOTENCY_LENGTH,
        "parent_job_id": MAX_JOB_PARENT_LENGTH,
        "parent_session_id": MAX_JOB_PARENT_LENGTH,
    }
    payload: dict[str, str] = {}
    for name, limit in limits.items():
        if name not in req:
            continue
        value = req[name]
        if not isinstance(value, str) or not value.strip() or len(value) > limit:
            raise ValueError(
                f"{name} must be a non-empty string of at most {limit} characters"
            )
        if "/" in value or "\\" in value:
            raise ValueError(f"{name} must not contain path separators")
        payload[name] = value
    return payload


def job_subroute_id(path: str, suffix: str, *, unquote: Callable[[str], str]) -> str | None:
    """Extract a safely encoded job id from ``/v1/jobs/<id><suffix>``."""
    prefix = "/v1/jobs/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    encoded = path[len(prefix) : -len(suffix)]
    if not encoded or "/" in encoded:
        return None
    job_id = unquote(encoded)
    return job_id if job_id and "/" not in job_id else None


def job_record_id(path: str, *, unquote: Callable[[str], str]) -> str | None:
    """Return one safely encoded job id from the direct read route."""
    prefix = "/v1/jobs/"
    if not path.startswith(prefix):
        return None
    encoded = path[len(prefix) :]
    if not encoded or "/" in encoded:
        return None
    job_id = unquote(encoded)
    return job_id if job_id and "/" not in job_id else None


def job_stream_query(
    raw_query: str,
    *,
    parse_qs: Callable[..., dict[str, list[str]]],
) -> tuple[int, int, int]:
    """Parse bounded ``after``, ``max_events``, and ``max_bytes`` stream params."""
    query = parse_qs(raw_query, keep_blank_values=True)

    def bounded(name: str, default: int, lower: int, upper: int) -> int:
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
        bounded("after", 0, 0, JOB_STREAM_MAX_AFTER),
        bounded("max_events", JOB_STREAM_DEFAULT_EVENTS, 1, JOB_STREAM_MAX_EVENTS),
        bounded("max_bytes", JOB_STREAM_DEFAULT_BYTES, 1, JOB_STREAM_MAX_BYTES),
    )


def job_output_event_payload(event: Any) -> dict[str, Any]:
    """Serialize one durable job output event."""
    spill = event.spill
    return {
        "sequence": event.watermark.sequence,
        "stream": event.stream.value,
        "data": event.data,
        "spill": (
            None
            if spill is None
            else {
                "digest": spill.digest,
                "preview": spill.preview,
                "size": spill.size,
                "mime_type": spill.mime_type,
                "owner_id": spill.owner_id,
            }
        ),
    }


@dataclass
class ConversationState:
    """Mutable state for one hosted conversation, guarded by its lock."""

    trace: bool = False
    strict: object = None
    persona: str = "auto"
    last_iid: str | None = None
    last_response: str | None = None
    last_run_source: str | None = None
    token: str = ""
    account: dict[str, Any] | None = None
    events: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)
    session_pins: int = 0


@dataclass(frozen=True)
class TurnResult:
    """Model-backed HTTP chat turn result and internal capture carrier."""

    content: str
    iid: str | None
    run_source: str
    thinking: str = ""
    resolved_model: str = ""
    resolved_tier: str = ""
    cache: str = ""
    provider_capture: tuple[Any, Any] | None = None


class LegacyConversationState:
    """Adapter preserving direct helper/REPL-style module-global behavior on ``serve``."""

    lock = threading.RLock()

    def __init__(self, serve_mod: Any) -> None:
        self._serve = serve_mod

    @property
    def trace(self) -> Any:
        return self._serve.TRACE

    @trace.setter
    def trace(self, value: Any) -> None:
        self._serve.TRACE = value

    @property
    def strict(self) -> Any:
        return self._serve.STRICT

    @strict.setter
    def strict(self, value: Any) -> None:
        self._serve.STRICT = value

    @property
    def persona(self) -> Any:
        return self._serve.PERSONA

    @persona.setter
    def persona(self, value: Any) -> None:
        self._serve.PERSONA = value

    @property
    def last_iid(self) -> Any:
        return self._serve.LAST_IID

    @last_iid.setter
    def last_iid(self, value: Any) -> None:
        self._serve.LAST_IID = value

    @property
    def last_response(self) -> Any:
        return self._serve.LAST_RESPONSE

    @last_response.setter
    def last_response(self, value: Any) -> None:
        self._serve.LAST_RESPONSE = value

    @property
    def last_run_source(self) -> Any:
        return self._serve.LAST_RUN_SOURCE

    @last_run_source.setter
    def last_run_source(self, value: Any) -> None:
        self._serve.LAST_RUN_SOURCE = value

    @property
    def token(self) -> Any:
        return self._serve.CURRENT_TOKEN

    @token.setter
    def token(self, value: Any) -> None:
        self._serve.CURRENT_TOKEN = value

    @property
    def account(self) -> Any:
        return self._serve.CURRENT_ACCOUNT

    @account.setter
    def account(self, value: Any) -> None:
        self._serve.CURRENT_ACCOUNT = value

    @property
    def events(self) -> Any:
        return self._serve.CHAT_EVENTS


def configure_memory_replication_service_impl(
    serve_mod: Any,
    service: Any,
    *,
    close_replaced: bool = True,
) -> Any:
    """Install one already-owned local replication service at HTTP startup."""
    if service is not None:
        for name in ("start", "receiver", "close", "status"):
            if not callable(getattr(service, name, None)):
                raise TypeError("memory replication service has an invalid lifecycle")
        service.start()
        receiver = service.receiver()
        if receiver is not None and not callable(getattr(receiver, "receive_bytes", None)):
            raise TypeError("memory replication service receiver is invalid")
    else:
        receiver = None

    previous = serve_mod._MEMORY_REPLICATION_SERVICE
    serve_mod.configure_memory_replication_receiver(receiver)
    serve_mod._MEMORY_REPLICATION_SERVICE = service
    if close_replaced and previous is not None and previous is not service:
        previous.close()
    return service


def apply_typed_config(
    serve_mod: Any,
    config: Any,
    *,
    artifact_binding_cls: Any,
    app_control_binding_cls: Any,
    control_plane_paths_cls: Any,
    live_control_plane_inventory_fn: Any,
    default_app_fn: Callable[[], Any],
    listener_probe_configure: Callable[[Any], None],
    web_app_root_fn: Callable[[Any], Any],
) -> None:
    """Bind validated ``SonderConfig`` values onto the ``serve`` HTTP boundary."""
    server_config = config.server
    serve_mod._require_exact_bind_values(
        server_config.host,
        config.secrets.api_key,
        server_config.auth_mode,
        config.secrets.auth_secret,
        require_account=server_config.require_account,
    )
    if type(server_config.tls_terminated_by_proxy) is not bool:
        raise RuntimeError("bind TLS proxy declaration must be a boolean")
    if type(getattr(server_config, "public_hosting", False)) is not bool:
        raise RuntimeError("public hosting declaration must be a boolean")
    logger = serve_mod._serve_logger
    logger.debug("configure_typed_config: binding server config to HTTP boundary")
    logger.info(
        f"Applying typed server configuration, host={config.server.host!r}, "
        f"port={config.server.port}, auth_mode={config.server.auth_mode!r}"
    )

    candidate = artifact_binding_cls(lambda: config)
    candidate.start()
    try:
        candidate_control = app_control_binding_cls(
            lambda: config,
            account_open=lambda: serve_mod.server._open_db(),
            account_path=lambda: Path(serve_mod.server._DB_PATH).resolve(),
            private_inventory=lambda: live_control_plane_inventory_fn(
                additional=lambda: control_plane_paths_cls(
                    databases=(Path(serve_mod.server._DB_PATH).resolve(),),
                    files=tuple(Path(p) for p in config.private_source_paths),
                )
            ),
            lanes_provider=lambda: default_app_fn().agent_lanes(),
        )
        candidate_control.start()
    except BaseException:
        candidate.close()
        raise

    previous = serve_mod._ARTIFACT_TRANSFER_BINDING
    serve_mod._ARTIFACT_TRANSFER_CONFIG = config
    candidate._config_provider = lambda: serve_mod._ARTIFACT_TRANSFER_CONFIG
    serve_mod._ARTIFACT_TRANSFER_BINDING = candidate
    if previous is not None:
        previous.close()

    serve_mod._APP_CONTROL_CONFIG = config
    candidate_control._config_provider = lambda: serve_mod._APP_CONTROL_CONFIG
    serve_mod._APP_CONTROL_BINDING = candidate_control

    listener_probe_configure(config)
    serve_mod._SPANDA_CONFIG = config.spanda
    serve_mod.CONFIGURED_PORT = server_config.port
    serve_mod.API_KEY = config.secrets.api_key
    serve_mod.AUTH_SECRET = config.secrets.auth_secret
    serve_mod.HOST = server_config.host
    serve_mod.REQUIRE_ACCOUNT = server_config.require_account
    configured_auth = (
        "account"
        if (serve_mod.REQUIRE_ACCOUNT and server_config.auth_mode == "api-key")
        else server_config.auth_mode
    )
    serve_mod.AUTH_MODE = serve_mod._keyless_loopback_auth_mode(
        configured_auth, serve_mod.API_KEY, serve_mod.HOST,
    )
    serve_mod.CORS_ORIGINS = frozenset(server_config.cors_origins)
    serve_mod.ALLOWED_HOSTS = serve_mod._parse_allowed_hosts(server_config.allowed_hosts)
    serve_mod._WEB_APP = WebAppSettings(
        server_config.web_app_enabled, web_app_root_fn(config),
    )
    serve_mod.OBSERVATORY_ORIGINS = frozenset(
        origin
        for origin in config.observability.live_export_origins
        if origin and origin != "*"
    )
    serve_mod.TLS_TERMINATED_BY_PROXY = server_config.tls_terminated_by_proxy
    serve_mod.ALLOW_REGISTRATION = server_config.allow_registration
    serve_mod.PUBLIC_HOSTING = bool(getattr(server_config, "public_hosting", False))
    serve_mod.MAX_REQUEST_BYTES = max(
        1, min(16 * 1024 * 1024, server_config.max_request_bytes),
    )
    serve_mod.MAX_DISCARDED_BODY_BYTES = min(serve_mod.MAX_REQUEST_BYTES, 64 * 1024)
    serve_mod.REQUEST_TIMEOUT_SECONDS = max(5, server_config.request_timeout_seconds)
    serve_mod.STREAM_IDLE_TIMEOUT_SECONDS = max(
        1, server_config.stream_idle_timeout_seconds,
    )
    serve_mod.STREAM_HEARTBEAT_SECONDS = max(
        1, min(300, server_config.stream_heartbeat_seconds),
    )
    serve_mod.PREWARM_ON_START = server_config.prewarm_on_start
    serve_mod.HTTP_SESSION_STATE_LIMIT = max(
        2, min(1024, server_config.session_state_limit),
    )
    serve_mod.HTTP_SESSION_STATE_OWNER_LIMIT = max(
        1,
        min(
            serve_mod.HTTP_SESSION_STATE_LIMIT - 1,
            server_config.session_state_owner_limit,
        ),
    )
    serve_mod.TRAIN_MAX_N = max(1, server_config.train_max_n)
    serve_mod._WORK_RUNNER.configure(
        wait_seconds=server_config.work_wait_seconds,
        budget_seconds=server_config.work_budget_seconds,
        max_running=server_config.work_max_running,
        max_live=server_config.work_max_live,
    )
    serve_mod._HEALTH_STATUS_FACADE = HealthStatusFacade(
        metrics_path=config.observability.metrics_path,
    )
    serve_mod._TRUSTED_PROXY_NETWORKS = tuple(
        ipaddress.ip_network(cidr, strict=False)
        for cidr in server_config.trusted_proxy_cidrs
    )
    serve_mod._detach_memory_replication_route()


__all__ = [
    "ConversationState",
    "DuplicateJsonObjectKey",
    "HTTPRequestError",
    "JOB_STREAM_DEFAULT_BYTES",
    "JOB_STREAM_DEFAULT_EVENTS",
    "JOB_STREAM_MAX_AFTER",
    "JOB_STREAM_MAX_BYTES",
    "JOB_STREAM_MAX_EVENTS",
    "LOCAL_LOG_PAGE",
    "LOCAL_LOG_TAIL_BYTES",
    "LegacyConversationState",
    "LiveSessionCaptureFailure",
    "MAX_JOB_CANCEL_REASON",
    "MAX_JOB_IDEMPOTENCY_LENGTH",
    "MAX_JOB_ID_LENGTH",
    "MAX_JOB_KIND_LENGTH",
    "MAX_JOB_OPERATION_LENGTH",
    "MAX_JOB_PARENT_LENGTH",
    "TurnResult",
    "apply_typed_config",
    "capture_live_session_turn",
    "chat_routing_metadata",
    "compute_job_route",
    "configure_memory_replication_service_impl",
    "fail_live_session_request",
    "job_cancel_id",
    "job_output_event_payload",
    "job_record_id",
    "job_record_payload",
    "job_start_payload",
    "job_stream_query",
    "job_subroute_id",
    "local_server_log_tail",
    "reject_duplicate_json_object_key",
    "reject_nonfinite_json_number",
]
