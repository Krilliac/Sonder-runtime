"""HTTP handling for the credential routes that spend a login-throttle budget.

``/v1/sonder/register`` and ``/v1/sonder/login`` reserve an auth-attempt token
at admission (``Handler._auth_reserve_refused``); the functions here
refund it on every outcome that is not a failed guess.  ``handler`` is the
request ``Handler`` instance and every call runs on its request thread.
"""
import logging
import sqlite3

import sonder_runtime.adapters.web.lifecycle as sonder_lifecycle
from sonder_runtime.adapters.security.login_throttle import LoginThrottled

# Same logger the routes used while they lived in serve.py.
_serve_logger = logging.getLogger("sonder_runtime.interfaces.http.serve")


def send_throttled(handler, retry_after=2):
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


def _record_failure(handler, reason):
    # The reservation taken at admission already paid for this failure.
    sonder_lifecycle.get().record_auth_failure(
        handler._client_ip(), reason, charge=False
    )


def _refund(handler):
    sonder_lifecycle.get().refund_auth_attempt(handler._client_ip())


def handle_register(handler, req, context, *, runtime, admin_auth,
                    allow_registration, created_message):
    if handler._auth_reserve_refused():
        return
    conn = runtime._open_db()
    try:
        account = admin_auth.register(
            conn,
            req.get("username", ""),
            req.get("password", ""),
            trusted_local=False,
            bootstrap_secret=handler.headers.get("X-Sonder-Bootstrap-Secret", ""),
            allow_additional=allow_registration,
            actor=context["account"] if context["authorized"] else None,
        )
        _refund(handler)
        # ``message`` lets a client that only reads ``ok``/``message``
        # (older apps) report the success instead of a bare 201.
        handler._send_json_payload({
            "ok": True, "account": account,
            "message": created_message(account),
        }, status=201)
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


def handle_login(handler, req, context, *, runtime, admin_auth):
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
        handler._send_json_payload({"ok": True, "token": token, "account": account})
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
