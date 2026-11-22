"""HTTP handling for the credential routes that spend a login-throttle budget.

``/v1/sonder/register`` and ``/v1/sonder/login`` reserve an auth-attempt token
at admission (``Handler._auth_reserve_refused``); the functions here
refund it on every outcome that is not a failed guess.  ``handler`` is the
request ``Handler`` instance and every call runs on its request thread.
"""
import logging
import math
import sqlite3
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Mapping
from typing import Any

import sonder_runtime.adapters.web.lifecycle as sonder_lifecycle
from sonder_runtime.adapters.security.login_throttle import LoginThrottled

# Same logger the routes used while they lived in serve.py.
_serve_logger = logging.getLogger("sonder_runtime.interfaces.http.serve")

PUBLIC_REGISTER_WINDOW_SECONDS = 60.0
PUBLIC_REGISTER_MAX_PER_WINDOW = 10
_PUBLIC_REGISTER_MAX_IPS = 4096
_REG_LOCK = threading.Lock()
_REG_ATTEMPTS: OrderedDict[str, list[float]] = OrderedDict()


def reset_registration_throttle() -> None:
    """Clear the per-IP public registration window (tests only)."""
    with _REG_LOCK:
        _REG_ATTEMPTS.clear()


def _check_and_record_public_registration(
    client_ip: str,
    *,
    max_per_window: int = PUBLIC_REGISTER_MAX_PER_WINDOW,
    window_seconds: float = PUBLIC_REGISTER_WINDOW_SECONDS,
) -> int:
    """Return 0 when allowed (recording the attempt), else retry_after seconds."""
    key = str(client_ip or "").strip() or "unknown"
    now = time.monotonic()
    cutoff = now - max(1.0, float(window_seconds))
    limit = max(1, int(max_per_window))
    with _REG_LOCK:
        stamps = [ts for ts in _REG_ATTEMPTS.get(key, ()) if ts > cutoff]
        if len(stamps) >= limit:
            oldest = stamps[0]
            _REG_ATTEMPTS[key] = stamps
            _REG_ATTEMPTS.move_to_end(key)
            return max(1, int(math.ceil((oldest + float(window_seconds)) - now)))
        stamps.append(now)
        _REG_ATTEMPTS[key] = stamps
        _REG_ATTEMPTS.move_to_end(key)
        while len(_REG_ATTEMPTS) > _PUBLIC_REGISTER_MAX_IPS:
            _REG_ATTEMPTS.popitem(last=False)
    return 0


def send_throttled(handler: Any, retry_after: int = 2) -> None:
    handler._send_json_payload(
        sonder_lifecycle.error_envelope(
            "AUTH_RATE_LIMITED",
            "too many failed authentication attempts; retry later",
            handler._correlation(),
            retryable=True,
        ),
        status=429,
        headers={"Retry-After": str(retry_after)},
    )


def _record_failure(handler: Any, reason: str) -> None:
    # The reservation taken at admission already paid for this failure.
    sonder_lifecycle.get().record_auth_failure(
        handler._client_ip(), reason, charge=False
    )


def _refund(handler: Any) -> None:
    sonder_lifecycle.get().refund_auth_attempt(handler._client_ip())


def handle_register(handler: Any, req: Mapping[str, Any], context: Mapping[str, Any], *,
                    runtime: Any, admin_auth: Any, allow_registration: bool,
                    created_message: Callable[[Any], str],
                    public_hosting: bool = False,
                    extra_fields: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
    if public_hosting:
        retry_after = _check_and_record_public_registration(handler._client_ip())
        if retry_after > 0:
            sonder_lifecycle.get().record_auth_failure(
                handler._client_ip(), "registration-throttled", charge=False,
            )
            send_throttled(handler, retry_after)
            return
    if handler._auth_reserve_refused():
        return
    conn = runtime._open_db()
    try:
        account = admin_auth.register(
            conn,
            req.get("username", ""),
            req.get("password", ""),
            trusted_local=False,
            bootstrap_secret="" if public_hosting else handler.headers.get("X-Sonder-Bootstrap-Secret", ""),
            allow_additional=allow_registration,
            actor=context["account"] if context["authorized"] else None,
            public_hosting=public_hosting,
        )
        _refund(handler)
        # ``message`` lets a client that only reads ``ok``/``message``
        # (older apps) report the success instead of a bare 201.
        body: dict[str, Any] = {
            "ok": True,
            "account": account,
            "message": created_message(account),
        }
        if extra_fields is not None:
            body.update(dict(extra_fields(account)))
        handler._send_json_payload(body, status=201)
    except PermissionError as error:
        _record_failure(handler, "registration-denied")
        handler._send_json_payload({"ok": False, "message": str(error)}, status=403)
    except sqlite3.IntegrityError:
        _refund(handler)
        handler._send_json_payload({"ok": False, "message": "account already exists"}, status=409)
    except ValueError as error:
        _refund(handler)
        handler._send_json_payload({"ok": False, "message": str(error)}, status=400)
    except Exception as error:
        _refund(handler)
        _serve_logger.error(f"account registration failed, correlation={handler._correlation()!r}", exc_info=True)
        handler.log_error("registration failed: %s", type(error).__name__)
        handler._send_json_payload({"ok": False, "message": "registration failed"}, status=500)
    finally:
        conn.close()


def handle_login(handler: Any, req: Mapping[str, Any], context: Mapping[str, Any], *,
                 runtime: Any, admin_auth: Any,
                 extra_fields: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None) -> None:
    if context["mode"] in ("api-key", "both") and not context["api_key"]:
        handler._send_auth_error()
        return
    if handler._auth_reserve_refused():
        return
    conn = runtime._open_db()
    try:
        token, account = admin_auth.login(
            conn, req.get("username", ""), req.get("password", ""),
            client_ip=handler._client_ip(),
        )
        _refund(handler)
        body: dict[str, Any] = {"ok": True, "token": token, "account": account}
        if extra_fields is not None:
            body.update(dict(extra_fields(account)))
        handler._send_json_payload(body)
    except LoginThrottled as error:
        # Refused before any password work: not a guess, so it does not spend
        # the caller's budget.
        _refund(handler)
        _record_failure(handler, "login-throttled")
        send_throttled(handler, error.retry_after_seconds)
    except (ValueError, PermissionError):
        _record_failure(handler, "invalid-credentials")
        handler._send_json_payload(
            {"ok": False, "message": "invalid username or password"}, status=401
        )
    except Exception as error:
        _refund(handler)
        _serve_logger.error(f"login failed, correlation={handler._correlation()!r}", exc_info=True)
        handler.log_error("login failed: %s", type(error).__name__)
        handler._send_json_payload({"ok": False, "message": "login failed"}, status=500)
    finally:
        conn.close()
