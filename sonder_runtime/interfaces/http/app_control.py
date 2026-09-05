"""Early bounded wire boundary for private app-control metadata."""

from contextlib import contextmanager
import re
import threading
import time
from ...application.ports.app_control_http import ControlError

_SLOTS = threading.BoundedSemaphore(8)
_LOCK = threading.Lock()
_ACTIVE = {}
_RATE = {}


def is_app_control_route(path):
    return isinstance(path, str) and path.startswith("/v1/app-control")


@contextmanager
def _admit(peer):
    if not _SLOTS.acquire(blocking=False):
        raise ControlError(429, "APP_CONTROL_BUSY")
    held = False
    try:
        now = time.monotonic()
        with _LOCK:
            for key in tuple(_RATE):
                if _RATE[key][1] + 60 <= now:
                    _RATE.pop(key)
            if peer not in _RATE and len(_RATE) >= 1024:
                raise ControlError(429, "APP_CONTROL_BUSY")
            count, started = _RATE.get(peer, (0, now))
            if count >= 30 or _ACTIVE.get(peer, 0) >= 2:
                raise ControlError(429, "APP_CONTROL_BUSY")
            _RATE[peer] = (count + 1, started)
            _ACTIVE[peer] = _ACTIVE.get(peer, 0) + 1
            held = True
        yield
    finally:
        if held:
            with _LOCK:
                _ACTIVE[peer] -= 1
                if not _ACTIVE[peer]:
                    _ACTIVE.pop(peer)
        _SLOTS.release()


def _header(handler, name, required=False):
    values = handler.headers.get_all(name) or ()
    if len(values) > 1 or required and len(values) != 1:
        raise ControlError(400, "INVALID_APP_CONTROL_HEADERS")
    value = values[0] if values else None
    if value is not None and (
        len(value) > 1024 or any(not 32 <= ord(c) <= 126 for c in value)
    ):
        raise ControlError(400, "INVALID_APP_CONTROL_HEADERS")
    return value


def _token(value):
    if value is None:
        raise ControlError(401, "APP_CONTROL_AUTH_REQUIRED")
    token = value[7:] if value.startswith("Bearer ") else value
    if not 1 <= len(token) <= 512 or any(not 33 <= ord(c) <= 126 for c in token):
        raise ControlError(401, "APP_CONTROL_AUTH_REQUIRED")
    return token


def _route(method, target):
    if (
        len(target) > 1024
        or "%" in target
        or any(not 33 <= ord(c) <= 126 for c in target)
    ):
        raise ControlError(400, "INVALID_APP_CONTROL_ROUTE")
    path, separator, query = target.partition("?")
    post = {
        "/enroll": "enroll",
        "/bindings": "create_binding",
        "/select": "select_binding",
        "/clear": "clear_selection",
        "/revoke": "revoke_binding",
        "/work": "prepare_work",
    }
    suffix = path.removeprefix("/v1/app-control")
    if method == "POST" and suffix in post and not separator:
        return post[suffix], {}
    work = re.fullmatch(r"/work/([0-9a-f]{64})(/execute)?", suffix)
    if work is not None and not separator:
        if method == "GET" and work[2] is None:
            return "read_work", {"work_id": work[1]}
        if method == "POST" and work[2] == "/execute":
            return "execute_work", {"work_id": work[1]}
    if method == "GET" and suffix == "/selection" and not separator:
        return "read_selection", {}
    if method == "GET" and suffix in ("/bindings", "/recovery"):
        values = {}
        if separator:
            for pair in query.split("&"):
                key, equals, value = pair.partition("=")
                if (
                    not equals
                    or key in values
                    or key not in {"binding_id", "after_position", "limit"}
                ):
                    raise ControlError(400, "INVALID_APP_CONTROL_QUERY")
                if key == "binding_id":
                    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
                        raise ControlError(400, "INVALID_APP_CONTROL_QUERY")
                    values[key] = value
                else:
                    if not re.fullmatch(r"[0-9]{1,18}", value):
                        raise ControlError(400, "INVALID_APP_CONTROL_QUERY")
                    values[key] = int(value)
        return ("list_bindings" if suffix == "/bindings" else "recovery"), values
    raise ControlError(404, "APP_CONTROL_ROUTE_NOT_FOUND")


def handle_app_control(
    handler, method, binding, *, deployment_authorized, work_binding=None
):
    if not is_app_control_route(handler.path):
        return False
    handler._app_control_request = True

    published = False

    def reply(status, body):
        nonlocal published
        if published:
            handler.close_connection = True
            return False
        published = True
        try:
            sent = handler._send_json_payload(
                body,
                status=status,
                headers={
                    "Cache-Control": "no-store",
                    "Referrer-Policy": "no-referrer",
                    "Pragma": "no-cache",
                },
            )
            if sent is False:
                handler.close_connection = True
            return sent
        except Exception:
            # A response may already be partially visible. Never write another
            # envelope or rerun enrollment after any writer failure.
            handler.close_connection = True
            return False

    try:
        peer = handler._peer()
        with _admit(peer):
            if binding is None or binding.store is None:
                raise ControlError(503, "APP_CONTROL_UNAVAILABLE")
            config = binding._config()
            origin = _header(handler, "Origin")
            authorization = _header(handler, "Authorization")
            account = _header(handler, "X-Sonder-Account-Token")
            credential = _header(handler, "X-Sonder-App-Control")
            _header(handler, "Content-Type")
            _header(handler, "Content-Length")
            if _header(handler, "Transfer-Encoding") is not None:
                raise ControlError(400, "INVALID_APP_CONTROL_HEADERS")
            # Validate the actual bound listener, not just a configured label.
            listener = handler.server.server_address[0]
            if not binding.transport_allowed(
                listener=listener, raw_peer=peer, origin=origin
            ):
                raise ControlError(403, "APP_CONTROL_TRANSPORT_REFUSED")
            if method == "OPTIONS":
                if handler.headers.get("Content-Length", "0") != "0":
                    raise ControlError(400, "INVALID_APP_CONTROL_REQUEST")
                reply(200, {"ok": True})
                return True
            action, payload = _route(method, handler.path)
            if not deployment_authorized(authorization or "", config):
                raise ControlError(401, "APP_CONTROL_AUTH_REQUIRED")
            account = _token(account)
            if method == "POST":
                body = handler._read_json(max_bytes=16384)
                if action == "execute_work":
                    if type(body) is not dict or body:
                        raise ControlError(400, "INVALID_APP_CONTROL_REQUEST")
                else:
                    payload = body
            elif handler.headers.get("Content-Length", "0") != "0":
                raise ControlError(400, "INVALID_APP_CONTROL_REQUEST")
            target = binding
            if action in {"prepare_work", "execute_work", "read_work"}:
                try:
                    target = work_binding() if callable(work_binding) else work_binding
                except Exception:
                    raise ControlError(503, "APP_WORK_UNAVAILABLE") from None
                if target is None or target.control is not binding:
                    raise ControlError(503, "APP_WORK_UNAVAILABLE")
            target.perform(
                action,
                payload,
                account_token=account,
                control_token=credential or "",
                publish=reply,
            )
    except (BrokenPipeError, ConnectionError, OSError):
        handler.close_connection = True
    except Exception as error:
        if hasattr(error, "status") and hasattr(error, "code"):
            status, code = error.status, error.code
        elif hasattr(error, "status"):
            status, code = error.status, "INVALID_APP_CONTROL_REQUEST"
        else:
            status, code = 503, "APP_CONTROL_UNAVAILABLE"
        reply(status, {"ok": False, "error": {"code": code}})
    return True
