"""Early bounded wire boundary for private app-control metadata."""

from contextlib import ExitStack, contextmanager
import json
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
        "/api-keys/list": "list_api_keys",
        "/api-keys/generate": "generate_api_key",
        "/api-keys/revoke": "revoke_api_key",
        "/swarm/status": "get_swarm_status",
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
    recovery_prepare = re.fullmatch(r"/work/([0-9a-f]{64})/recovery", suffix)
    if method == "POST" and recovery_prepare is not None and not separator:
        return "prepare_recovery", {"work_id": recovery_prepare[1]}
    recovery = re.fullmatch(
        r"/recovery/([0-9a-f]{64})(/(attach|resume|close))?", suffix
    )
    if recovery is not None and not separator:
        if method == "GET" and recovery[2] is None:
            return "inspect_recovery", {"attempt_id": recovery[1]}
        if method == "POST" and recovery[3] is not None:
            return recovery[3] + "_recovery", {"attempt_id": recovery[1]}
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


# Answered here rather than by ``binding.perform``: they need an account
# session but no enrolled control credential or project grant.
_ACCOUNT_ACTIONS = frozenset(
    {"list_api_keys", "generate_api_key", "revoke_api_key", "get_swarm_status"}
)
_KEY_ID_CHARS = frozenset("0123456789abcdef")


def _account_action(binding, action, token, body, accounts, swarm_status):
    """``(status, body)`` for the caller's own API keys or the admin swarm view.

    The identity is the account session ``token`` names, read from the
    binding's configured accounts database, never the header text itself.
    ``get_swarm_status`` is an operator view and takes the same admin-session
    check as every other app-control action (``binding._account``); the key
    actions take any live session and act only on that session's account.
    ``accounts`` (the account engine) and ``swarm_status`` (a callable listing
    agent rows) are supplied by the composing server; without them these
    routes are unavailable.
    """
    body = {} if body is None else body
    if type(body) is not dict or set(body) - (
        {"key_id"} if action == "revoke_api_key" else set()
    ):
        raise ControlError(400, "INVALID_APP_CONTROL_REQUEST")
    key_id = body.get("key_id")
    if key_id is not None and (
        type(key_id) is not str
        or len(key_id) != 16
        or not set(key_id) <= _KEY_ID_CHARS
    ):
        raise ControlError(400, "INVALID_APP_CONTROL_REQUEST")
    if accounts is None or swarm_status is None:
        raise ControlError(503, "APP_CONTROL_UNAVAILABLE")
    account_auth = accounts
    conn = binding._open()
    try:
        if action == "get_swarm_status":
            binding._account(conn, token)
            rows = swarm_status()
            return 200, {"ok": True, "status": json.loads(json.dumps(rows, default=str))}
        binding._config()
        binding._source(conn)
        identity = account_auth.authenticate_session(conn, token)
        binding._source(conn)
        if identity is None:
            raise ControlError(401, "APP_CONTROL_AUTH_REQUIRED")
        username = identity.username
        if action == "list_api_keys":
            return 200, {"ok": True, "keys": account_auth.list_api_keys(conn, username)}
        if action == "generate_api_key":
            try:
                minted = account_auth.generate_api_key(conn, username)
            except ValueError:
                raise ControlError(409, "API_KEY_LIMIT_REACHED") from None
            return 200, {"ok": True, **minted}
        revoked = account_auth.revoke_api_key(conn, username, key_id)
        if key_id is not None and not revoked:
            raise ControlError(404, "API_KEY_NOT_FOUND")
        return 200, {"ok": True, "revoked": revoked}
    finally:
        conn.close()


def handle_app_control(
    handler, method, binding, *, deployment_authorized, work_binding=None,
    accounts=None, swarm_status=None
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
        with _admit(peer), ExitStack() as scopes:
            if binding is None or binding.store is None:
                raise ControlError(503, "APP_CONTROL_UNAVAILABLE")
            scopes.enter_context(binding._private_inventory_scope())
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
            if action in _ACCOUNT_ACTIONS:
                body = handler._read_json(max_bytes=16384)
                reply(*_account_action(
                    binding, action, account, body, accounts, swarm_status
                ))
                return True
            if method == "POST":
                body = handler._read_json(max_bytes=16384)
                if action in {
                    "execute_work",
                    "attach_recovery",
                    "resume_recovery",
                    "close_recovery",
                }:
                    if type(body) is not dict or body:
                        raise ControlError(400, "INVALID_APP_CONTROL_REQUEST")
                elif action == "prepare_recovery":
                    if type(body) is not dict or set(body) != {
                        "attachment_command_id",
                        "completion_command_id",
                    }:
                        raise ControlError(400, "INVALID_APP_CONTROL_REQUEST")
                    payload.update(body)
                else:
                    payload = body
            elif handler.headers.get("Content-Length", "0") != "0":
                raise ControlError(400, "INVALID_APP_CONTROL_REQUEST")
            target = binding
            if action in {
                "prepare_work",
                "execute_work",
                "read_work",
                "prepare_recovery",
                "inspect_recovery",
                "attach_recovery",
                "resume_recovery",
                "close_recovery",
            }:
                try:
                    target = work_binding() if callable(work_binding) else work_binding
                except Exception:
                    raise ControlError(503, "APP_WORK_UNAVAILABLE") from None
                if target is None or target.control is not binding:
                    raise ControlError(503, "APP_WORK_UNAVAILABLE")
            if action.endswith("_recovery"):
                target = target.recovery_http()
                if target is None:
                    raise ControlError(503, "APP_RECOVERY_UNAVAILABLE")
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
