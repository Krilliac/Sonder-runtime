"""Loopback HTTP companion server for client-side tool execution."""
from __future__ import annotations

import hmac
import json
import secrets
import socketserver
import threading
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from wsgiref.types import StartResponse, WSGIEnvironment

from .executor import (
    CLIENT_AGENT_MODES,
    CLIENT_DELEGATABLE_TOOLS,
    ClientAgentPolicy,
    execute_client_tool,
)

DEFAULT_CLIENT_AGENT_HOST = "127.0.0.1"
DEFAULT_CLIENT_AGENT_PORT = 11439
_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})


def is_loopback_host(host: str) -> bool:
    clean = host.strip().lower()
    if clean in _LOOPBACK_HOSTS:
        return True
    if clean.startswith("127."):
        parts = clean.split(".")
        return len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    return False


def normalize_origin(raw: str) -> str | None:
    """Validate and normalize an ``http(s)://host[:port]`` origin without ``urllib``."""
    text = raw.strip()
    if not text or text.lower() == "null":
        return None
    if any(ch in text for ch in ("?", "#", "@", " ", "\t", "\r", "\n")):
        return None
    lower = text.lower()
    if lower.startswith("https://"):
        scheme = "https"
        rest = text[8:]
    elif lower.startswith("http://"):
        scheme = "http"
        rest = text[7:]
    else:
        return None
    if "/" in rest:
        rest = rest.rstrip("/")
        if "/" in rest:
            return None
    if not rest:
        return None
    if rest.startswith("["):
        end_bracket = rest.find("]")
        if end_bracket <= 1:
            return None
        host = rest[: end_bracket + 1].lower()
        after = rest[end_bracket + 1 :]
        if after:
            if not after.startswith(":") or not after[1:].isdigit():
                return None
            port = int(after[1:])
            if not 1 <= port <= 65535:
                return None
            return f"{scheme}://{host}:{port}"
        return f"{scheme}://{host}"
    if ":" in rest:
        host_part, port_part = rest.rsplit(":", 1)
        if not host_part or not port_part.isdigit():
            return None
        port = int(port_part)
        if not 1 <= port <= 65535:
            return None
        return f"{scheme}://{host_part.lower()}:{port}"
    return f"{scheme}://{rest.lower()}"


def _origin_host(normalized_origin: str) -> str:
    after_scheme = normalized_origin.split("://", 1)[1]
    if after_scheme.startswith("["):
        end = after_scheme.find("]")
        return after_scheme[: end + 1]
    return after_scheme.split(":", 1)[0]


def is_origin_allowed(origin: str, allowed_origins: frozenset[str]) -> bool:
    normalized = normalize_origin(origin)
    if normalized is None:
        return False
    if normalized in allowed_origins:
        return True
    host = _origin_host(normalized)
    return is_loopback_host(host)


class _QuietWSGIHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: object) -> None:
        return


class ClientAgentServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True
    token: str = ""
    policy: ClientAgentPolicy
    allowed_origins: frozenset[str] = frozenset()


def _extract_token(environ: Mapping[str, Any]) -> str:
    direct = str(environ.get("HTTP_X_SONDER_CLIENT_AGENT_TOKEN") or "").strip()
    if direct:
        return direct
    auth = str(environ.get("HTTP_AUTHORIZATION") or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return ""


def _read_json_body(environ: Mapping[str, Any]) -> tuple[dict[str, Any] | None, int]:
    raw_len = str(environ.get("CONTENT_LENGTH") or "").strip()
    if not raw_len:
        return {}, 200
    try:
        length = int(raw_len)
    except ValueError:
        return None, 400
    if length < 0:
        return None, 400
    if length > _MAX_REQUEST_BODY_BYTES:
        return None, 413
    if length == 0:
        return {}, 200
    stream = environ.get("wsgi.input")
    if stream is None or not hasattr(stream, "read"):
        return None, 400
    data = stream.read(length)
    if not isinstance(data, (bytes, bytearray)):
        return None, 400
    try:
        decoded = json.loads(bytes(data).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, 400
    if not isinstance(decoded, dict):
        return None, 400
    return decoded, 200


_MODE_RANK: dict[str, int] = {
    "plan": 0,
    "manual": 1,
    "acceptEdits": 2,
    "auto": 3,
    "bypass": 4,
}


def build_wsgi_app(
    *,
    policy: ClientAgentPolicy,
    token: str,
    allowed_origins: Iterable[str] = (),
) -> Callable[[WSGIEnvironment, StartResponse], Iterable[bytes]]:
    """Build the WSGI callable for the local client companion."""
    normalized_origins = frozenset(
        norm for raw in allowed_origins if (norm := normalize_origin(raw)) is not None
    )
    lock = threading.Lock()

    def _cors_headers(origin_header: str) -> list[tuple[str, str]]:
        if not origin_header:
            return []
        normalized = normalize_origin(origin_header)
        if normalized is None:
            return []
        headers = [
            ("Access-Control-Allow-Origin", origin_header.strip()),
            ("Vary", "Origin"),
            (
                "Access-Control-Allow-Headers",
                "Content-Type, Authorization, X-Sonder-Client-Agent-Token",
            ),
            ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"),
            ("Access-Control-Allow-Private-Network", "true"),
        ]
        return headers

    def _respond(
        start_response: StartResponse,
        status: int,
        payload: Mapping[str, Any] | None,
        origin_header: str = "",
    ) -> list[bytes]:
        status_text = {
            200: "200 OK",
            204: "204 No Content",
            400: "400 Bad Request",
            401: "401 Unauthorized",
            403: "403 Forbidden",
            404: "404 Not Found",
            405: "405 Method Not Allowed",
            413: "413 Payload Too Large",
        }.get(status, f"{status} Error")
        headers: list[tuple[str, str]] = [
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
            *_cors_headers(origin_header),
        ]
        if payload is None:
            start_response(status_text, headers)
            return [b""]
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        headers.extend([
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(body))),
        ])
        start_response(status_text, headers)
        return [body]

    def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "/")
        origin_header = str(environ.get("HTTP_ORIGIN") or "").strip()

        if origin_header and not is_origin_allowed(origin_header, normalized_origins):
            return _respond(
                start_response,
                403,
                {"ok": False, "error": {"code": "ORIGIN_FORBIDDEN", "message": "Origin not allowed"}},
            )

        if method == "OPTIONS":
            return _respond(start_response, 204, None, origin_header)

        if method == "GET" and path == "/health":
            return _respond(
                start_response,
                200,
                {
                    "ok": True,
                    "service": "sonder-client-agent",
                    "version": 1,
                    "auth_required": True,
                },
                origin_header,
            )

        supplied_token = _extract_token(environ)
        if not supplied_token or not hmac.compare_digest(supplied_token, token):
            return _respond(
                start_response,
                401,
                {
                    "ok": False,
                    "error": {
                        "code": "UNAUTHORIZED",
                        "message": "Valid X-Sonder-Client-Agent-Token or Bearer token required",
                    },
                },
                origin_header,
            )

        if method == "GET" and path == "/v1/client-agent/status":
            with lock:
                mode = policy.mode
                workspace = str(policy.workspace_root)
            return _respond(
                start_response,
                200,
                {
                    "ok": True,
                    "service": "sonder-client-agent",
                    "mode": mode,
                    "modes": list(CLIENT_AGENT_MODES),
                    "workspace": workspace,
                    "can_bypass": True,
                    "tools": sorted(CLIENT_DELEGATABLE_TOOLS),
                },
                origin_header,
            )

        if method == "POST" and path == "/v1/client-agent/mode":
            body, body_status = _read_json_body(environ)
            if body is None:
                return _respond(
                    start_response,
                    body_status,
                    {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "Invalid JSON body"}},
                    origin_header,
                )
            wanted = str(body.get("mode") or "").strip()
            confirm_bypass = body.get("confirm_bypass") is True
            try:
                with lock:
                    updated = policy.set_mode(wanted, confirm_bypass=confirm_bypass)
                    workspace = str(policy.workspace_root)
            except PermissionError as exc:
                return _respond(
                    start_response,
                    400,
                    {
                        "ok": False,
                        "error": {"code": "BYPASS_CONFIRMATION_REQUIRED", "message": str(exc)},
                    },
                    origin_header,
                )
            except ValueError as exc:
                return _respond(
                    start_response,
                    400,
                    {"ok": False, "error": {"code": "INVALID_MODE", "message": str(exc)}},
                    origin_header,
                )
            return _respond(
                start_response,
                200,
                {"ok": True, "mode": updated, "workspace": workspace, "can_bypass": True},
                origin_header,
            )

        if method == "POST" and path == "/v1/client-agent/execute":
            body, body_status = _read_json_body(environ)
            if body is None:
                return _respond(
                    start_response,
                    body_status,
                    {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "Invalid JSON body"}},
                    origin_header,
                )
            tool = str(body.get("tool") or "").strip()
            if not tool:
                return _respond(
                    start_response,
                    400,
                    {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "tool is required"}},
                    origin_header,
                )
            raw_args = body.get("args")
            if raw_args is not None and not isinstance(raw_args, Mapping):
                return _respond(
                    start_response,
                    400,
                    {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "args must be an object"}},
                    origin_header,
                )
            approved = body.get("approved") is True
            requested_mode = str(body.get("mode") or "").strip()
            with lock:
                effective_policy = ClientAgentPolicy(
                    workspace_root=policy.workspace_root,
                    mode=policy.mode,
                )
                # Caller may request a stricter mode per call, never a looser one.
                if (
                    requested_mode in _MODE_RANK
                    and _MODE_RANK[requested_mode] < _MODE_RANK[policy.mode]
                ):
                    effective_policy.mode = requested_mode
            result = execute_client_tool(
                tool,
                raw_args,
                policy=effective_policy,
                approved=approved,
            )
            payload = result.to_dict()
            call_id = str(body.get("call_id") or "").strip()
            if call_id:
                payload["call_id"] = call_id
            return _respond(start_response, 200, payload, origin_header)

        if path in {"/health", "/v1/client-agent/status", "/v1/client-agent/mode", "/v1/client-agent/execute"}:
            return _respond(
                start_response,
                405,
                {"ok": False, "error": {"code": "METHOD_NOT_ALLOWED", "message": "Method not allowed"}},
                origin_header,
            )
        return _respond(
            start_response,
            404,
            {"ok": False, "error": {"code": "NOT_FOUND", "message": f"Unknown path: {path}"}},
            origin_header,
        )

    return app


def create_client_agent_server(
    *,
    host: str = DEFAULT_CLIENT_AGENT_HOST,
    port: int = DEFAULT_CLIENT_AGENT_PORT,
    workspace_root: Path | str | None = None,
    mode: str = "manual",
    token: str | None = None,
    allowed_origins: Iterable[str] = (),
) -> ClientAgentServer:
    """Create a loopback-bound ``ClientAgentServer`` ready to ``serve_forever()``."""
    clean_host = host.strip() or DEFAULT_CLIENT_AGENT_HOST
    if not is_loopback_host(clean_host):
        raise ValueError(f"client_agent must bind to a loopback address, not {clean_host!r}")
    effective_token = (token or "").strip() or secrets.token_urlsafe(32)
    policy = ClientAgentPolicy(workspace_root=workspace_root, mode=mode)
    normalized_origins = frozenset(
        norm for raw in allowed_origins if (norm := normalize_origin(raw)) is not None
    )
    app = build_wsgi_app(policy=policy, token=effective_token, allowed_origins=normalized_origins)
    srv = make_server(
        clean_host,
        int(port),
        app,
        server_class=ClientAgentServer,
        handler_class=_QuietWSGIHandler,
    )
    # Attach metadata directly on the server instance for callers/tests.
    if isinstance(srv, ClientAgentServer):
        srv.token = effective_token
        srv.policy = policy
        srv.allowed_origins = normalized_origins
        return srv
    raise RuntimeError("unexpected WSGI server class")
