"""HTTP side of the Observatory live telemetry: routes and the chat turn.

* ``GET /.well-known/sonder-telemetry`` -- discovery;
* ``GET /v1/observability/events`` -- the SSE / NDJSON stream;
* ``GET /v1/sonder/ecosystem`` -- provider bindings and Observatory status;
* the live turn of one ``POST /v1/chat/completions`` (begin / finish).

The routes are admin-gated exactly like ``/v1/observability/trace`` and a
stream subscriber never holds a request admission slot.  ``handler`` is
serve.py's request handler (auth check, JSON sender, CORS and socket); the
application lookup, the Host policy, the lifecycle, the version, query
parsing and the logger are injected by ``serve.py``, so this module imports
no bootstrap, platform, socket or urllib module.  Wire framing lives in
``observability_stream``.
"""
from __future__ import annotations

import re
import time
from typing import Any, Callable, Mapping, Sequence

from ....application.observability.ecosystem_status import build_ecosystem_status
from ....application.ports.telemetry_feed import SubscriberLimitReached
from .observability_stream import (
    DISCOVERY_ROUTE,
    ECOSYSTEM_ROUTE,
    EVENTS_ROUTE,
    StreamRequestError,
    negotiate_stream,
    stream_frames,
)

# Longer than the default drain deadline (25 s), so the flush hook -- not
# this backstop -- normally ends the telemetry streams.
STREAM_DRAIN_BACKSTOP_SECONDS = 35.0

_TELEMETRY_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "[::1]"})
_LEGACY_ERROR_REPLY = re.compile(r"\AERROR(?::| contacting )")


def is_legacy_error_reply(content: object) -> bool:
    """True for the legacy ``ERROR: ...`` / ``ERROR contacting ...`` answers."""
    return isinstance(content, str) and bool(_LEGACY_ERROR_REPLY.match(content.lstrip()))


def chat_turn_outcome(result, error_kind, *, error_reply=False, failed_attempt_code=None,
                      http_status=None):
    """(outcome, error_code) of the terminal request.* event for one chat turn.

    ``result`` is the HTTP metric label.  A model failure reports its kind as
    a domain code (``cancelled`` becomes ``request.cancelled``).  A 200 whose
    body is a legacy error answer, or whose every provider attempt failed,
    is a failed turn: telemetry never calls a failed model call completed.
    """
    if error_kind:
        from ....application.session.provider_attempts import MODEL_ERROR_KIND_CODES

        code = MODEL_ERROR_KIND_CODES.get(str(error_kind), str(error_kind))
        if code == "INVALID_INPUT" and type(http_status) is int and http_status >= 500:
            # A configuration refusal answered 503 (for example web research
            # on a tier bound to another provider) is not the caller's error.
            code = "DEPENDENCY_UNAVAILABLE"
        return ("cancelled" if error_kind == "cancelled" else "failed"), code
    if result == "cancelled":
        return "cancelled", "CANCELLED"
    if result != "ok":
        return "failed", result
    if failed_attempt_code:
        return "failed", failed_attempt_code
    if error_reply:
        return "failed", "ERROR_REPLY"
    return "completed", None


def register_telemetry_drain(coordinator: Any, application: Any) -> bool:
    """Close the live telemetry export when a drain completes; True if registered.

    Streams hold no admission slot, so a drain would never wait for them; the
    flush hook ends them.  It runs the graph's own telemetry close when there
    is one, which emits ``session.ended`` (and a final ``telemetry.dropped``)
    before closing the subscriptions, so connected subscribers receive them.
    """
    feed = getattr(application, "telemetry_feed", None)
    if feed is None:
        return False
    close = getattr(application, "close_telemetry", None)
    coordinator.add_flush_hook(close if callable(close) else feed.close_subscribers)
    return True


def telemetry_host_allowed(value: object) -> bool:
    """True when a Host header names a loopback name (any port)."""
    host = str(value or "").strip().lower()
    if host.startswith("["):
        end = host.find("]")
        if end < 0:
            return False
        name, rest = host[: end + 1], host[end + 1:]
    else:
        name, _sep, rest = host.partition(":")
        rest = ":" + rest if _sep else ""
    if rest and not (rest.startswith(":") and rest[1:].isdigit()):
        return False
    return name in _TELEMETRY_LOOPBACK_HOSTS


def loopback_base_url(host: object, port: object) -> str:
    """The listener URL a same-host client uses (127.0.0.1 for 0.0.0.0)."""
    host = str(host or "").strip()
    if host in ("", "0.0.0.0"):
        host = "127.0.0.1"
    elif host in ("::", "[::]"):
        host = "[::1]"
    elif ":" in host and not host.startswith("["):
        host = "[%s]" % host
    return "http://%s:%s" % (host, port)


def ecosystem_document(application: Any, feed: Any, *, base: str, version: str,
                       generated_at: Any, node: str, observatory_origins: Sequence[str],
                       dedicated_origins: Sequence[str]) -> Mapping[str, Any] | None:
    """GET /v1/sonder/ecosystem body, or None when there is nothing to report."""
    if application is None:
        return None
    export_enabled = feed is not None
    stream = None
    if export_enabled:
        stream = {
            "discovery_url": base + DISCOVERY_ROUTE,
            "sse_url": base + EVENTS_ROUTE,
            "ndjson_url": base + EVENTS_ROUTE + "?format=ndjson",
        }
    return build_ecosystem_status(
        generated_at=generated_at,
        runtime={
            "version": version,
            "instance_id": getattr(feed, "instance_id", None),
            "node_id": getattr(feed, "node_id", None) or node,
        },
        bindings=application.provider_bindings,
        gateway=application.model_gateway,
        export_enabled=export_enabled,
        runtime_stream=stream,
        stats=feed.stats() if export_enabled else None,
        observatory_origins=sorted(observatory_origins),
        dedicated_origins=sorted(dedicated_origins),
        runtime_base_url=base,
    )


def begin_chat_turn(handler: Any, *, application: Any, bind_operation_context: Callable,
                    log: Any, model: Any, stream: Any, session_id: Any) -> None:
    """Bind the ambient context and start the Observatory turn (R = correlation id)."""
    stack = getattr(handler, "_turn_stack", None)
    if stack is None:
        return
    operation_context = getattr(handler, "_operation_context", None)
    if operation_context is not None:
        stack.enter_context(bind_operation_context(operation_context))
    telemetry = getattr(application, "telemetry", None)
    if telemetry is None:
        return
    try:
        turn = telemetry.begin_turn(
            turn_id=handler._correlation(),
            surface="http.chat_completions",
            stream=bool(stream),
            requested_model=model,
            source="http",
            session_id=session_id or None,
        )
        stack.enter_context(telemetry.activate(turn))
    except Exception:
        log.warning("live telemetry turn could not start", exc_info=True)
        return
    handler._telemetry_turn = (telemetry, turn)


def finish_chat_turn(handler: Any, *, log: Any) -> None:
    """Emit the single terminal request.* event for a started turn."""
    started = getattr(handler, "_telemetry_turn", None)
    handler._telemetry_turn = None
    if started is None:
        return
    telemetry, turn = started
    result = getattr(handler, "_telemetry_result", None) or "unrecorded"
    status = getattr(handler, "_last_response_status", None)
    outcome, error_code = chat_turn_outcome(
        result, getattr(handler, "_telemetry_error_kind", None),
        error_reply=bool(getattr(handler, "_telemetry_error_reply", False)),
        failed_attempt_code=turn.failed_attempts_code(),
        http_status=status,
    )
    try:
        telemetry.finish_turn(turn, outcome=outcome, http_status=status, error_code=error_code)
    except Exception:
        log.warning("live telemetry turn could not finish", exc_info=True)


def _admin_context(handler: Any, admin_authorized: Callable[[Any], bool]) -> Any:
    """Admin authorization exactly as for /v1/observability/trace."""
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return None
    if not admin_authorized(context):
        handler._send_json_payload(
            {"error": {"message": "administrator authorization is required",
                       "type": "forbidden", "code": "FORBIDDEN"}},
            status=403,
        )
        return None
    return context


def reject_rebound_host(handler: Any, *, loopback_listener: bool) -> bool:
    """DNS-rebinding defence for the telemetry routes on a loopback bind.

    A rebinding page is same-origin, so it sends no Origin and CORS never
    applies; its Host header still names the attacker's domain.  On a
    loopback listener (without a declared TLS proxy, which forwards its
    public name) a Host that is not 127.0.0.1, localhost or [::1] (any
    port) is refused with 403 forbidden_host, as Sonder-Inference does.
    """
    if not loopback_listener:
        return False
    raw = handler.headers.get("Host")
    if raw is None or telemetry_host_allowed(raw):
        return False
    handler._send_json_payload(
        {"error": {"message": "host is not allowed", "type": "forbidden",
                   "code": "forbidden_host"}},
        status=403,
    )
    return True


def serve_get(handler: Any, path: str, *, loopback_listener: bool,
              admin_authorized: Callable[[Any], bool], application_for: Callable[[], Any],
              ecosystem_for: Callable[[Any, Any], Any], auth_required: Callable[[], bool],
              stream: Callable[[Any], None]) -> None:
    """Serve one telemetry GET route (``path`` is in ``TELEMETRY_ROUTES``)."""
    if reject_rebound_host(handler, loopback_listener=loopback_listener):
        return
    if _admin_context(handler, admin_authorized) is None:
        return
    application = application_for()
    feed = getattr(application, "telemetry_feed", None)
    if path == ECOSYSTEM_ROUTE:
        document = ecosystem_for(application, feed)
        if document is None:
            handler._send_not_found()
            return
        handler._send_json_payload(document, headers={"Cache-Control": "no-store"})
        return
    if feed is None:
        # SONDER_OBSERVATORY_EXPORT=0: the telemetry routes do not exist.
        handler._send_not_found()
        return
    if path == DISCOVERY_ROUTE:
        handler._send_json_payload(
            dict(feed.discovery(auth_required=auth_required())),
            headers={"Cache-Control": "no-store"},
        )
        return
    stream(feed)


def stream_telemetry(handler: Any, feed: Any, *, query: Mapping[str, Sequence[str]],
                     lifecycle: Any, idle_timeout: float,
                     peer_closed: Callable[[Any], bool]) -> None:
    """Serve one SSE/NDJSON subscriber; never holds a request admission slot."""
    try:
        request = negotiate_stream(
            query,
            accept=handler.headers.get("Accept", ""),
            last_event_id_header=handler.headers.get("Last-Event-ID"),
        )
    except StreamRequestError as error:
        handler._send_json_payload(
            {"error": {"message": str(error), "type": "invalid_request"}}, status=400,
        )
        return
    try:
        subscription = feed.subscribe(
            last_event_id=request.last_event_id, since_now=request.since_now,
        )
    except SubscriberLimitReached as error:
        handler._send_json_payload(
            {"error": {"message": str(error), "type": "rate_limit_error",
                       "code": "TOO_MANY_SUBSCRIBERS"}},
            status=429,
            headers={"Retry-After": "2"},
        )
        return
    drain_seen: list[float] = []

    def should_stop() -> bool:
        # A drain lets admitted turns finish; the drain's flush hook then
        # emits session.ended and closes this subscription, so the stream
        # shows the in-flight turns ending.  This is only the backstop for
        # a drain whose flush hooks never run.
        coordinator = getattr(lifecycle, "coordinator", None)
        if not getattr(coordinator, "draining", False):
            return False
        if not drain_seen:
            drain_seen.append(time.monotonic())
        return time.monotonic() - drain_seen[0] > STREAM_DRAIN_BACKSTOP_SECONDS

    try:
        handler._close_for_unread_body()
        handler.close_connection = True
        handler.send_response(200)
        handler._cors()
        handler.send_header("Content-Type", request.content_type)
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("Connection", "close")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        handler.wfile.flush()
        connection = getattr(handler, "connection", None)
        if connection is not None:
            # A subscriber that stops reading must not pin this thread:
            # a blocked write fails after the stream idle timeout.
            connection.settimeout(idle_timeout)
        for frame in stream_frames(
            subscription, request.format, should_stop=should_stop,
            peer_closed=lambda: peer_closed(connection),
        ):
            handler.wfile.write(frame)
            handler.wfile.flush()
    except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError, TimeoutError, OSError):
        # The subscriber left or stalled past the idle timeout.
        pass
    finally:
        subscription.close()


__all__ = [
    "STREAM_DRAIN_BACKSTOP_SECONDS",
    "begin_chat_turn",
    "chat_turn_outcome",
    "ecosystem_document",
    "finish_chat_turn",
    "is_legacy_error_reply",
    "loopback_base_url",
    "register_telemetry_drain",
    "reject_rebound_host",
    "serve_get",
    "stream_telemetry",
    "telemetry_host_allowed",
]
