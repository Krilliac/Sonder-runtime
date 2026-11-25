"""Pure HTTP chat-tool SSE frame/waiter, slash message, and permission-mode helpers."""
from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

from sonder_runtime.application.chat.bidi import detect_bidi_chars

DEFAULT_APPROVAL_WAIT_SECONDS = 120.0
_WAIT_POLL_INTERVAL_SECONDS = 0.25
_MIN_POLL_INTERVAL_SECONDS = 0.005
_MAX_POLL_INTERVAL_SECONDS = 1.0
_WAITER_LOCK = threading.Lock()
_CALL_WAITERS: dict[str, set[threading.Event]] = {}


def slash_messages(
    messages: Any,
    history: Any,
    history_from_messages: Callable[[Any], Any],
) -> Any:
    """The transcript a slash command reads: the request's, else the session's."""
    if history and not history_from_messages(messages):
        return [*history, *(messages or ())]
    return messages


BYPASS_HTTP_LOCAL_OPEN_REFUSAL = (
    "bypass mode over HTTP needs an authenticated administrator account (or "
    "the owner API key); this server's auth is local-open, so every local "
    "process reaches it unauthenticated as the same caller. Configure "
    "accounts or an API key, or enter bypass at the attended Sonder console "
    "(/mode bypass, then confirm)."
)

ESCALATION_HTTP_LOCAL_OPEN_REFUSAL = (
    "raising the permission mode above manual (to %s) over HTTP needs an "
    "authenticated administrator account (or the owner API key); this "
    "server's auth is local-open, so every local process reaches it "
    "unauthenticated as the same caller. Configure accounts or an API key, "
    "or raise it at the attended Sonder console (/mode %s). Lowering the "
    "mode to manual or plan is allowed here."
)


def is_local_open_context(context: Any) -> bool:
    """True when this request is unauthenticated local-open.

    That is ``local-open`` as configured, an ``api-key`` deployment with no
    key on loopback (downgraded to it), or a context that does not say which
    auth mode is in force (fails closed). Every local process is then the
    same unauthenticated caller, so nothing here may raise autonomy or issue
    authority: no mode above manual, no bypass, no one-shot approval.
    """
    if not isinstance(context, Mapping):
        return True
    mode = str(context.get("mode") or "").strip().lower()
    return not mode or mode == "local-open"


def request_mode_scope(permission_policy: Any, context: Any, principal: str, session: str) -> Any:
    """``permission_policy.mode_scope`` for one HTTP request, capped at manual
    when the request is local-open (see ``is_local_open_context``)."""
    return permission_policy.mode_scope(
        principal, session, local_open=is_local_open_context(context),
    )


def bypass_http_refusal(context: Any) -> str:
    """``""`` when this HTTP caller may enter ``bypass``, else why not.

    ``confirm_bypass`` is a JSON field any caller can send, so it confirms
    nothing about who is present. Under ``local-open`` (configured, or an
    ``api-key`` deployment with no key on loopback, which is downgraded to
    it) every unauthenticated local process is "admin" and shares one
    principal, so a bypass entered here would apply to all of them. Fails
    closed on a context that does not say which auth mode is in force.
    """
    return BYPASS_HTTP_LOCAL_OPEN_REFUSAL if is_local_open_context(context) else ""


def handle_permission_mode_get(
    handler: Any,
    *,
    permission_policy: Any,
    principal_of: Callable[[Any], str],
    authority_error: Callable[[str, Any], str],
    observe_state: Callable[[], None],
    mode_data: Callable[[], dict[str, Any]],
) -> None:
    """Serve ``GET /v1/permission-mode`` with principal/session scope and ``can_bypass``."""
    context = handler._request_auth_context()
    if not context["authorized"]:
        handler._send_auth_error()
        return
    session_id = str(handler.headers.get("X-Sonder-Session", "") or "").strip()
    can_bypass = not (
        authority_error("permission_mode_change", context)
        or bypass_http_refusal(context)
    )
    with permission_policy.mode_scope(
        principal_of(context), session_id, local_open=is_local_open_context(context),
    ):
        observe_state()
        payload = dict(mode_data())
    payload["can_bypass"] = can_bypass
    handler._send_json_payload(payload)


def _policy_modes(permission_policy: Any) -> list[str]:
    modes_fn = getattr(permission_policy, "modes", None)
    return list(modes_fn()) if callable(modes_fn) else list(getattr(permission_policy, "MODES", ()))


def handle_permission_mode_post(
    handler: Any,
    req: Any,
    context: Any,
    *,
    permission_policy: Any,
    principal_of: Callable[[Any], str],
    idempotent_action: Callable[..., Any],
    send_idempotency_refusal: Callable[[Any, Any], bool],
    observe_state: Callable[[], None],
    mode_data: Callable[[], dict[str, Any]],
    approver_of: Callable[[Any], str],
    audit_transition: Callable[..., None] | None = None,
    error_envelope: Callable[..., dict[str, Any]] | None = None,
    send_escalation_refusal: Callable[..., bool] | None = None,
) -> None:
    """Serve ``POST /v1/permission-mode`` with gated ``bypass`` entry and audit.

    Under local-open auth raising the mode above both current and manual is
    refused with 403 (``PERMISSION_MODE_ESCALATION_REFUSED``, or
    ``BYPASS_REQUIRES_AUTHENTICATED_ADMIN`` for bypass) while lowering remains
    allowed; authenticated admins keep every mode, with non-bypass modes global
    (clearing per-session scopes as a safety stop) and bypass scoped to
    principal/session behind ``confirm_bypass: true``.
    """
    wanted = ""
    confirm_bypass = False
    session_id = str(handler.headers.get("X-Sonder-Session", "") or "").strip()
    if isinstance(req, dict):
        wanted = str(req.get("mode") or "").strip()
        confirm_bypass = req.get("confirm_bypass") is True
        if not session_id:
            session_id = str(req.get("session_id") or "").strip()
    if not wanted:
        handler._send_json_payload(
            {"error": "mode is required", "modes": _policy_modes(permission_policy)},
            status=400,
        )
        return
    try:
        resolved = permission_policy.resolve_mode(wanted)
    except ValueError as exc:
        handler._send_json_payload(
            {"error": str(exc), "modes": _policy_modes(permission_policy)},
            status=400,
        )
        return
    local_open_refusal = bypass_http_refusal(context)
    if local_open_refusal and resolved == getattr(permission_policy, "BYPASS", "bypass"):
        handler._send_json_payload(
            {
                "error": local_open_refusal,
                "code": "BYPASS_REQUIRES_AUTHENTICATED_ADMIN",
                "modes": _policy_modes(permission_policy),
            },
            status=403,
        )
        return
    if resolved == getattr(permission_policy, "BYPASS", "bypass") and not confirm_bypass:
        handler._send_json_payload(
            {
                "error": "bypass mode requires explicit confirmation (confirm_bypass: true)",
                "code": "BYPASS_CONFIRMATION_REQUIRED",
                "modes": _policy_modes(permission_policy),
            },
            status=400,
        )
        return
    principal = principal_of(context)
    scoped = resolved == getattr(permission_policy, "BYPASS", "bypass")
    local_open = is_local_open_context(context)
    refused_type = getattr(permission_policy, "ModeEscalationRefused", None) or permission_policy.mode_escalation_refused()
    if local_open:
        def apply_mode() -> Any:
            try:
                return permission_policy.set_mode_without_escalation(resolved)
            except refused_type as refused:
                return refused
    else:
        change = permission_policy.set_mode if scoped else permission_policy.set_global_mode

        def apply_mode() -> Any:
            return change(resolved)

    try:
        with permission_policy.mode_scope(principal, session_id, local_open=local_open):
            previous = permission_policy.current_mode()
            result = idempotent_action(
                context,
                handler.headers.get("Idempotency-Key", ""),
                f"permission-mode\0{principal}\0{session_id}\0{resolved}",
                apply_mode,
            )
    except ValueError as exc:
        handler._send_json_payload(
            {"error": str(exc), "modes": _policy_modes(permission_policy)},
            status=400,
        )
        return
    if send_idempotency_refusal(handler, result):
        return
    if error_envelope is None:
        def error_envelope(code: str, message: str, correlation_id: str, *, retryable: bool = False) -> dict[str, Any]:
            return {"error": {"code": code, "message": message, "correlation_id": correlation_id, "retryable": retryable}}
    if send_escalation_refusal is not None:
        if send_escalation_refusal(handler, permission_policy, result, error_envelope):
            return
    elif isinstance(result, refused_type):
        current = str(getattr(result, "current", ""))
        target = str(getattr(result, "target", resolved))
        payload = error_envelope(
            "PERMISSION_MODE_ESCALATION_REFUSED",
            ESCALATION_HTTP_LOCAL_OPEN_REFUSAL % (target, target),
            handler._correlation(),
            retryable=False,
        )
        payload["mode"] = current
        handler._send_json_payload(payload, status=403)
        return
    if audit_transition is not None and previous != resolved and (previous == "bypass" or resolved == "bypass"):
        audit_transition(
            previous, resolved, surface="http", actor=approver_of(context),
            confirmed=confirm_bypass,
        )
    if scoped:
        with permission_policy.mode_scope(principal, session_id, local_open=local_open):
            observe_state()
            payload = dict(mode_data())
    else:
        observe_state()
        payload = dict(mode_data())
        if isinstance(result, str) and result:
            payload["mode"] = result
    payload["can_bypass"] = not bypass_http_refusal(context)
    handler._send_json_payload(payload)


def turn_refusal_receipt(
    refusals: Any,
    permission_policy: Any,
    refusal_receipt_fn: Callable[..., Any],
) -> Any:
    """Build the structured refusal receipt for the last nameable refusal of a turn."""
    if not refusals:
        return None
    decision = refusals[-1]
    try:
        label = permission_policy.mode_label(decision.mode)
    except Exception:
        label = str(decision.mode or "")
    try:
        modes = list(permission_policy._modes_allowing(decision.risk))
    except Exception:
        modes = []
    return refusal_receipt_fn(decision, mode_label=label, modes_allowing=modes, count=len(refusals))


def _normalize_waiter_key(call_id: Any) -> str:
    if not isinstance(call_id, str):
        return ""
    key = call_id.strip().lower()
    if not key or len(key) > 256:
        return ""
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in key):
        return ""
    return key


def register_call_waiter(call_id: str) -> threading.Event:
    """Register an event woken when ``call_id`` is approved, denied, or cancelled."""
    key = _normalize_waiter_key(call_id)
    event = threading.Event()
    if key:
        with _WAITER_LOCK:
            _CALL_WAITERS.setdefault(key, set()).add(event)
    return event


def unregister_call_waiter(call_id: str, event: threading.Event | None = None) -> None:
    key = _normalize_waiter_key(call_id)
    if not key:
        return
    with _WAITER_LOCK:
        waiters = _CALL_WAITERS.get(key)
        if not waiters:
            return
        if event is None:
            _CALL_WAITERS.pop(key, None)
            return
        waiters.discard(event)
        if not waiters:
            _CALL_WAITERS.pop(key, None)


def notify_call_decision(call_id: str) -> None:
    """Wake every chat-tool turn waiting on ``call_id``."""
    key = _normalize_waiter_key(call_id)
    if not key:
        return
    with _WAITER_LOCK:
        events = tuple(_CALL_WAITERS.get(key, ()))
    for event in events:
        event.set()


def cancel_call_waiters(call_id: str | None = None) -> int:
    """Wake waiting approval event(s) immediately on turn cancellation."""
    if call_id is not None:
        key = _normalize_waiter_key(call_id)
        if not key:
            return 0
        with _WAITER_LOCK:
            events = tuple(_CALL_WAITERS.get(key, ()))
    else:
        with _WAITER_LOCK:
            events = tuple(ev for group in _CALL_WAITERS.values() for ev in group)
    for event in events:
        event.set()
    return len(events)


def write_tool_chunk(
    handler: Any,
    chunk: Callable[..., str],
    *,
    iid: str,
    model: str,
    payload: Mapping[str, Any],
) -> bool:
    """Write one ``sonder_tool`` SSE frame through ``early.lock``."""
    return _write_early_chunk(handler, chunk, iid=iid, model=model, field="sonder_tool", payload=payload)


def write_agent_chunk(
    handler: Any,
    chunk: Callable[..., str],
    *,
    iid: str,
    model: str,
    payload: Mapping[str, Any],
) -> bool:
    """Write one ``sonder_agent`` SSE frame through ``early.lock``."""
    return _write_early_chunk(handler, chunk, iid=iid, model=model, field="sonder_agent", payload=payload)


def write_agent_board_chunk(
    handler: Any,
    chunk: Callable[..., str],
    *,
    iid: str,
    model: str,
    payload: Mapping[str, Any],
) -> bool:
    """Write one ``sonder_agent_board`` SSE frame through ``early.lock``."""
    return _write_early_chunk(handler, chunk, iid=iid, model=model, field="sonder_agent_board", payload=payload)


def _write_early_chunk(
    handler: Any,
    chunk: Callable[..., str],
    *,
    iid: str,
    model: str,
    field: str,
    payload: Mapping[str, Any],
) -> bool:
    early = getattr(handler, "_early_stream", None)
    if early is None or getattr(early, "client_gone", False):
        return False
    lock = getattr(early, "lock", None)
    if lock is None:
        return False
    with lock:
        if getattr(early, "client_gone", False):
            return False
        wfile = getattr(handler, "wfile", None)
        if wfile is None:
            early.client_gone = True
            return False
        frame_id = getattr(early, "completion_id", None) or iid
        if not getattr(early, "completion_id", None):
            early.completion_id = frame_id
        try:
            frame = chunk(frame_id, model, {}, **{field: dict(payload)}).encode("utf-8")
        except (TypeError, ValueError):
            return False
        try:
            wfile.write(frame)
            flush = getattr(wfile, "flush", None)
            if callable(flush):
                flush()
        except (OSError, ValueError):
            early.client_gone = True
            return False
    return True


def _safe_wait_timeout(timeout_seconds: Any) -> float:
    if isinstance(timeout_seconds, bool):
        return DEFAULT_APPROVAL_WAIT_SECONDS
    try:
        value = float(timeout_seconds)
    except (TypeError, ValueError):
        return DEFAULT_APPROVAL_WAIT_SECONDS
    if not math.isfinite(value):
        return DEFAULT_APPROVAL_WAIT_SECONDS
    return max(0.05, value)


def _safe_poll_interval(poll_interval: Any) -> float:
    if isinstance(poll_interval, bool):
        return _WAIT_POLL_INTERVAL_SECONDS
    try:
        value = float(poll_interval)
    except (TypeError, ValueError):
        return _WAIT_POLL_INTERVAL_SECONDS
    if not math.isfinite(value) or value <= 0.0:
        return _WAIT_POLL_INTERVAL_SECONDS
    return max(_MIN_POLL_INTERVAL_SECONDS, min(_MAX_POLL_INTERVAL_SECONDS, value))


def _check_aborted(is_aborted: Callable[[], bool] | None) -> bool:
    if is_aborted is None:
        return False
    try:
        return bool(is_aborted())
    except Exception:
        return True


def wait_for_approval(
    ledger: Any,
    tool: str,
    digest: str,
    call_id: str,
    *,
    timeout_seconds: float = DEFAULT_APPROVAL_WAIT_SECONDS,
    is_aborted: Callable[[], bool] | None = None,
    poll_interval: float = _WAIT_POLL_INTERVAL_SECONDS,
) -> str:
    """Wait up to ``timeout_seconds`` for ``call_id`` approval or denial."""
    if _check_aborted(is_aborted):
        return "aborted"
    if ledger.open_for(tool, digest):
        return "approved"
    if ledger.denied_for(tool, digest):
        return "denied"
    safe_timeout = _safe_wait_timeout(timeout_seconds)
    safe_poll = _safe_poll_interval(poll_interval)
    event = register_call_waiter(call_id)
    deadline = time.monotonic() + safe_timeout
    try:
        while True:
            event.clear()
            if _check_aborted(is_aborted):
                return "aborted"
            if ledger.open_for(tool, digest):
                return "approved"
            if ledger.denied_for(tool, digest):
                return "denied"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "expired"
            event.wait(min(safe_poll, remaining))
    finally:
        unregister_call_waiter(call_id, event)


__all__ = [
    "BYPASS_HTTP_LOCAL_OPEN_REFUSAL",
    "DEFAULT_APPROVAL_WAIT_SECONDS",
    "ESCALATION_HTTP_LOCAL_OPEN_REFUSAL",
    "bypass_http_refusal",
    "cancel_call_waiters",
    "detect_bidi_chars",
    "handle_permission_mode_get",
    "handle_permission_mode_post",
    "is_local_open_context",
    "notify_call_decision",
    "register_call_waiter",
    "request_mode_scope",
    "slash_messages",
    "turn_refusal_receipt",
    "unregister_call_waiter",
    "wait_for_approval",
    "write_agent_board_chunk",
    "write_agent_chunk",
    "write_tool_chunk",
]
