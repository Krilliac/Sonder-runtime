"""Loopback HTTP companion server for client-side tool execution."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import socketserver
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server
from wsgiref.types import StartResponse, WSGIEnvironment

from .executor import (
    CLIENT_AGENT_MODE_RANK,
    CLIENT_AGENT_MODES,
    CLIENT_DELEGATABLE_TOOLS,
    ClientAgentPolicy,
    ModeCeilingRefused,
    execute_client_tool,
    mode_allows_without_prompt,
    tool_risk_of,
)

DEFAULT_CLIENT_AGENT_HOST = "127.0.0.1"
DEFAULT_CLIENT_AGENT_PORT = 11439
_MAX_REQUEST_BODY_BYTES = 2 * 1024 * 1024
_BODY_READ_CHUNK_BYTES = 65_536
_MAX_JSON_DEPTH = 64
_MAX_REQUEST_LINE_BYTES = 4_096
_MAX_HEADER_COUNT = 64
_MAX_HEADER_BYTES = 16_384
_REQUEST_READ_TIMEOUT_SECONDS = 30.0
_MAX_ORIGIN_CHARS = 512
_MAX_TOKEN_CHARS = 4_096
_MAX_CALL_ID_CHARS = 256
_MAX_APPROVAL_TOKEN_CHARS = 256
_MAX_TOOL_NAME_CHARS = 128
_MAX_MODE_CHARS = 64
_MAX_ERROR_CHARS = 4_096
_MAX_REFLECTED_PATH_CHARS = 128
_LOOPBACK_HOSTS: frozenset[str] = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
_DEFAULT_APPROVAL_TTL_SECONDS = 300.0
_MAX_APPROVAL_CHALLENGES = 256
_BEARER_SECRET_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{6,}")
_TOKEN_HEADER_SECRET_RE = re.compile(
    r"(?i)(X-Sonder-Client-Agent-Token\s*[:=]\s*)[^\s,;\"']+"
)


def _has_control_chars(text: str, *, allow_newlines: bool = False) -> bool:
    for ch in text:
        code = ord(ch)
        if allow_newlines and ch in ("\n", "\t"):
            continue
        if code < 32 or code == 127:
            return True
    return False


def _sanitize_error_message(
    text: str,
    *,
    secrets_to_redact: Iterable[str] = (),
    limit: int = _MAX_ERROR_CHARS,
) -> str:
    """Strip control characters, redact tokens/secrets, and bound error text."""
    cleaned = "".join(
        ch if (ord(ch) >= 32 and ord(ch) != 127) or ch in ("\n", "\t") else " "
        for ch in str(text or "")
    )
    cleaned = _BEARER_SECRET_RE.sub("Bearer [redacted]", cleaned)
    cleaned = _TOKEN_HEADER_SECRET_RE.sub(r"\1[redacted]", cleaned)
    for secret in secrets_to_redact:
        if secret and len(secret) >= 6:
            cleaned = cleaned.replace(secret, "[redacted]")
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 16)] + "\n...[truncated]"


def _reject_json_constant(constant: str) -> Any:
    raise ValueError(f"non-standard JSON constant {constant!r} is not allowed")


def _json_depth_within_limit(value: Any, max_depth: int) -> bool:
    """Iterative container nesting depth check (avoids Python recursion exhaustion)."""
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            return False
        if isinstance(current, dict):
            next_depth = depth + 1
            for item in current.values():
                if isinstance(item, (dict, list)):
                    stack.append((item, next_depth))
        elif isinstance(current, list):
            next_depth = depth + 1
            for item in current:
                if isinstance(item, (dict, list)):
                    stack.append((item, next_depth))
    return True


def canonical_args_digest(args: Mapping[str, Any] | None) -> str:
    """Return the SHA-256 hex digest of canonical JSON-encoded tool arguments."""
    payload = dict(args) if isinstance(args, Mapping) else {}
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(frozen=True)
class _ApprovalChallenge:
    token: str
    call_id: str
    tool: str
    args_digest: str
    expires_at: float


def is_loopback_host(host: str) -> bool:
    clean = host.strip().lower()
    if clean in _LOOPBACK_HOSTS:
        return True
    if clean.startswith("127."):
        parts = clean.split(".")
        return len(parts) == 4 and all(p.isascii() and p.isdigit() and 0 <= int(p) <= 255 for p in parts)
    return False


def _format_origin(scheme: str, host: str, port: int | None) -> str:
    if port is None:
        return f"{scheme}://{host}"
    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        return f"{scheme}://{host}"
    return f"{scheme}://{host}:{port}"


def normalize_origin(raw: str) -> str | None:
    """Validate and normalize an ``http(s)://host[:port]`` origin without ``urllib``."""
    text = raw.strip()
    if not text or len(text) > _MAX_ORIGIN_CHARS or text.lower() == "null":
        return None
    if _has_control_chars(text) or any(ch in text for ch in ("?", "#", "@", " ", "\\", "%")):
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
            if not after.startswith(":") or not (after[1:].isascii() and after[1:].isdigit()):
                return None
            port = int(after[1:])
            if not 1 <= port <= 65535:
                return None
            return _format_origin(scheme, host, port)
        return _format_origin(scheme, host, None)
    if ":" in rest:
        host_part, port_part = rest.rsplit(":", 1)
        if not host_part or ":" in host_part or not (port_part.isascii() and port_part.isdigit()):
            return None
        port = int(port_part)
        if not 1 <= port <= 65535:
            return None
        return _format_origin(scheme, host_part.lower(), port)
    return _format_origin(scheme, rest.lower(), None)


def is_allowed_host_header(raw_host: str) -> bool:
    """Return True iff an HTTP ``Host`` header names a valid loopback host[:port].

    Defends against DNS-rebinding attacks where a remote domain resolves to
    127.0.0.1 and issues same-origin browser requests without an ``Origin``
    mismatch.
    """
    text = raw_host.strip()
    if not text or len(text) > _MAX_ORIGIN_CHARS:
        return False
    if _has_control_chars(text) or any(ch in text for ch in (" ", "/", "\\", "@", "?", "#", "%", ",")):
        return False
    if text.startswith("["):
        end_bracket = text.find("]")
        if end_bracket <= 1:
            return False
        host = text[: end_bracket + 1].lower()
        after = text[end_bracket + 1 :]
        if after:
            if not after.startswith(":") or not (after[1:].isascii() and after[1:].isdigit()):
                return False
            port = int(after[1:])
            if not 1 <= port <= 65535:
                return False
        return is_loopback_host(host)
    if ":" in text:
        host_part, port_part = text.rsplit(":", 1)
        if not host_part or ":" in host_part or not (port_part.isascii() and port_part.isdigit()):
            return False
        port = int(port_part)
        if not 1 <= port <= 65535:
            return False
        return is_loopback_host(host_part)
    return is_loopback_host(text)


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
    timeout = _REQUEST_READ_TIMEOUT_SECONDS
    server_version = "sonder-client-agent"
    sys_version = ""

    def log_message(self, format: str, *args: object) -> None:
        return

    def _send_json_error(self, code: int, error_code: str, message: str) -> None:
        body = json.dumps(
            {"ok": False, "error": {"code": error_code, "message": message}},
            ensure_ascii=False,
        ).encode("utf-8")
        if not hasattr(self, "requestline"):
            self.requestline = ""
        if getattr(self, "request_version", "") in ("", "HTTP/0.9"):
            self.request_version = self.protocol_version
        self.send_response(code)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(body)
        except OSError:
            pass

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        _ = explain
        safe_msg = _sanitize_error_message(message or f"HTTP {code}", limit=256)
        error_code = {
            400: "INVALID_REQUEST",
            414: "URI_TOO_LONG",
            431: "REQUEST_HEADER_FIELDS_TOO_LARGE",
        }.get(int(code), "HTTP_ERROR")
        self._send_json_error(int(code), error_code, safe_msg)

    def parse_request(self) -> bool:
        raw_line = getattr(self, "raw_requestline", b"")
        if isinstance(raw_line, (bytes, bytearray)) and len(raw_line) > _MAX_REQUEST_LINE_BYTES:
            self.command = ""
            self.requestline = ""
            self.request_version = self.protocol_version
            self.close_connection = True
            self._send_json_error(
                414,
                "URI_TOO_LONG",
                f"Request line exceeds {_MAX_REQUEST_LINE_BYTES} bytes",
            )
            return False
        if not super().parse_request():
            return False
        raw_headers = self.headers.items()
        if len(raw_headers) > _MAX_HEADER_COUNT:
            self._send_json_error(
                431,
                "REQUEST_HEADER_FIELDS_TOO_LARGE",
                f"Request exceeds {_MAX_HEADER_COUNT} headers",
            )
            return False
        total_header_bytes = sum(len(k) + len(v) + 4 for k, v in raw_headers)
        if total_header_bytes > _MAX_HEADER_BYTES:
            self._send_json_error(
                431,
                "REQUEST_HEADER_FIELDS_TOO_LARGE",
                f"Request headers exceed {_MAX_HEADER_BYTES} bytes",
            )
            return False
        hosts = self.headers.get_all("host") or []
        if len(hosts) > 1:
            self._send_json_error(400, "INVALID_REQUEST", "Duplicate Host header is not allowed")
            return False
        content_lengths = self.headers.get_all("content-length") or []
        if len(content_lengths) > 1:
            self._send_json_error(
                400,
                "INVALID_REQUEST",
                "Duplicate Content-Length header is not allowed",
            )
            return False
        return True


class ClientAgentServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    allow_reuse_address = True
    token: str = ""
    policy: ClientAgentPolicy
    allowed_origins: frozenset[str] = frozenset()


class _ClientAgentIPv6Server(ClientAgentServer):
    address_family = vars(socketserver)["socket"].AF_INET6


def _extract_token(environ: Mapping[str, Any]) -> str:
    direct = str(environ.get("HTTP_X_SONDER_CLIENT_AGENT_TOKEN") or "").strip()
    if direct:
        if len(direct) > _MAX_TOKEN_CHARS or _has_control_chars(direct):
            return ""
        return direct
    auth = str(environ.get("HTTP_AUTHORIZATION") or "").strip()
    if auth.lower().startswith("bearer "):
        bearer = auth[7:].strip()
        if len(bearer) > _MAX_TOKEN_CHARS or _has_control_chars(bearer):
            return ""
        return bearer
    return ""


def _read_json_body(environ: Mapping[str, Any]) -> tuple[dict[str, Any] | None, int]:
    transfer_encoding = str(environ.get("HTTP_TRANSFER_ENCODING") or "").strip()
    raw_len = str(environ.get("CONTENT_LENGTH") or "").strip()
    if transfer_encoding:
        if not raw_len:
            return None, 411
        return None, 400
    if not raw_len:
        return {}, 200
    if not (raw_len.isascii() and raw_len.isdigit()):
        return None, 400
    length = int(raw_len)
    if length < 0:
        return None, 400
    if length > _MAX_REQUEST_BODY_BYTES:
        return None, 413
    if length == 0:
        return {}, 200
    stream = environ.get("wsgi.input")
    if stream is None or not hasattr(stream, "read"):
        return None, 400
    chunks: list[bytes] = []
    remaining = length
    while remaining > 0:
        piece = stream.read(min(_BODY_READ_CHUNK_BYTES, remaining))
        if not isinstance(piece, (bytes, bytearray)):
            return None, 400
        if not piece:
            break
        chunks.append(bytes(piece))
        remaining -= len(piece)
    if remaining > 0:
        return None, 400
    data = b"".join(chunks)
    try:
        decoded = json.loads(data.decode("utf-8"), parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, RecursionError):
        return None, 400
    if not isinstance(decoded, dict):
        return None, 400
    if not _json_depth_within_limit(decoded, _MAX_JSON_DEPTH):
        return None, 400
    return decoded, 200


_MODE_RANK: dict[str, int] = CLIENT_AGENT_MODE_RANK


def _body_error_payload(status: int) -> dict[str, Any]:
    if status == 413:
        return {
            "ok": False,
            "error": {
                "code": "PAYLOAD_TOO_LARGE",
                "message": f"Request body exceeds {_MAX_REQUEST_BODY_BYTES} bytes",
            },
        }
    if status == 411:
        return {
            "ok": False,
            "error": {
                "code": "LENGTH_REQUIRED",
                "message": "Content-Length header is required",
            },
        }
    return {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "Invalid JSON body"}}


def build_wsgi_app(
    *,
    policy: ClientAgentPolicy,
    token: str,
    allowed_origins: Iterable[str] = (),
    approval_ttl_seconds: float = _DEFAULT_APPROVAL_TTL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Callable[[WSGIEnvironment, StartResponse], Iterable[bytes]]:
    """Build the WSGI callable for the local client companion."""
    normalized_origins = frozenset(
        norm for raw in allowed_origins if (norm := normalize_origin(raw)) is not None
    )
    lock = threading.Lock()
    ttl_seconds = max(1.0, float(approval_ttl_seconds))
    challenges: list[_ApprovalChallenge] = []

    def _prune_challenges_locked(now: float) -> None:
        challenges[:] = [c for c in challenges if c.expires_at > now]

    def _mint_challenge_locked(
        call_id: str,
        clean_tool: str,
        args_digest: str,
    ) -> _ApprovalChallenge:
        now = float(clock())
        _prune_challenges_locked(now)
        while len(challenges) >= _MAX_APPROVAL_CHALLENGES:
            challenges.pop(0)
        challenge = _ApprovalChallenge(
            token=secrets.token_urlsafe(32),
            call_id=call_id,
            tool=clean_tool,
            args_digest=args_digest,
            expires_at=now + ttl_seconds,
        )
        challenges.append(challenge)
        return challenge

    def _consume_challenge_locked(
        approval_token: str,
        *,
        call_id: str,
        clean_tool: str,
        args_digest: str,
        has_supplied_digest: bool,
        supplied_args_digest: str,
    ) -> tuple[bool, str, bool]:
        now = float(clock())
        _prune_challenges_locked(now)
        token_bytes = approval_token.encode("utf-8")
        matched_idx = -1
        for idx, candidate in enumerate(challenges):
            if hmac.compare_digest(candidate.token.encode("utf-8"), token_bytes):
                matched_idx = idx
        if matched_idx < 0:
            return False, "approval token is invalid, expired, or already used", False
        matched = challenges.pop(matched_idx)
        call_id_ok = hmac.compare_digest(matched.call_id.encode("utf-8"), call_id.encode("utf-8"))
        tool_ok = hmac.compare_digest(matched.tool.encode("utf-8"), clean_tool.encode("utf-8"))
        digest_ok = hmac.compare_digest(matched.args_digest.encode("utf-8"), args_digest.encode("utf-8"))
        supplied_ok = (not has_supplied_digest) or hmac.compare_digest(
            matched.args_digest.encode("utf-8"),
            supplied_args_digest.encode("utf-8"),
        )
        if not (call_id_ok and tool_ok and digest_ok and supplied_ok):
            return False, "approval token does not match call_id, tool, or arguments", True
        return True, "", False

    def _cors_headers(origin_header: str) -> list[tuple[str, str]]:
        if not origin_header:
            return []
        normalized = normalize_origin(origin_header)
        if normalized is None:
            return []
        headers = [
            ("Access-Control-Allow-Origin", normalized),
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
            411: "411 Length Required",
            413: "413 Payload Too Large",
            414: "414 URI Too Long",
            431: "431 Request Header Fields Too Large",
            500: "500 Internal Server Error",
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

    def _handle_request(environ: WSGIEnvironment, start_response: StartResponse) -> list[bytes]:
        method = str(environ.get("REQUEST_METHOD") or "GET").upper()
        path = str(environ.get("PATH_INFO") or "/")
        origin_header = str(environ.get("HTTP_ORIGIN") or "").strip()
        host_header = str(environ.get("HTTP_HOST") or "").strip()
        protocol = str(environ.get("SERVER_PROTOCOL") or "").strip().upper()

        if host_header:
            if not is_allowed_host_header(host_header):
                return _respond(
                    start_response,
                    403,
                    {"ok": False, "error": {"code": "HOST_FORBIDDEN", "message": "Host header not allowed"}},
                )
        elif protocol == "HTTP/1.1":
            return _respond(
                start_response,
                403,
                {"ok": False, "error": {"code": "HOST_FORBIDDEN", "message": "Host header is required"}},
            )

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
        if not supplied_token or not hmac.compare_digest(
            supplied_token.encode("utf-8"),
            token.encode("utf-8"),
        ):
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
                max_mode = policy.max_mode
                can_bypass = policy.can_bypass
                workspace = str(policy.workspace_root)
            return _respond(
                start_response,
                200,
                {
                    "ok": True,
                    "service": "sonder-client-agent",
                    "mode": mode,
                    "max_mode": max_mode,
                    "modes": list(CLIENT_AGENT_MODES),
                    "workspace": workspace,
                    "can_bypass": can_bypass,
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
                    _body_error_payload(body_status),
                    origin_header,
                )
            raw_wanted = body.get("mode")
            if not isinstance(raw_wanted, str) or len(raw_wanted) > _MAX_MODE_CHARS or _has_control_chars(raw_wanted):
                return _respond(
                    start_response,
                    400,
                    {"ok": False, "error": {"code": "INVALID_MODE", "message": "mode must be a valid string"}},
                    origin_header,
                )
            wanted = raw_wanted.strip()
            raw_confirm = body.get("confirm_bypass")
            if raw_confirm is not None and not isinstance(raw_confirm, bool):
                return _respond(
                    start_response,
                    400,
                    {
                        "ok": False,
                        "error": {"code": "INVALID_REQUEST", "message": "confirm_bypass must be a boolean"},
                    },
                    origin_header,
                )
            confirm_bypass = raw_confirm is True
            try:
                with lock:
                    updated = policy.set_mode(wanted, confirm_bypass=confirm_bypass)
                    max_mode = policy.max_mode
                    can_bypass = policy.can_bypass
                    workspace = str(policy.workspace_root)
            except ModeCeilingRefused as exc:
                return _respond(
                    start_response,
                    403,
                    {
                        "ok": False,
                        "error": {
                            "code": "MODE_FORBIDDEN",
                            "message": _sanitize_error_message(str(exc), secrets_to_redact=(token,)),
                        },
                    },
                    origin_header,
                )
            except PermissionError as exc:
                return _respond(
                    start_response,
                    400,
                    {
                        "ok": False,
                        "error": {
                            "code": "BYPASS_CONFIRMATION_REQUIRED",
                            "message": _sanitize_error_message(str(exc), secrets_to_redact=(token,)),
                        },
                    },
                    origin_header,
                )
            except ValueError as exc:
                return _respond(
                    start_response,
                    400,
                    {
                        "ok": False,
                        "error": {
                            "code": "INVALID_MODE",
                            "message": _sanitize_error_message(str(exc), secrets_to_redact=(token,)),
                        },
                    },
                    origin_header,
                )
            return _respond(
                start_response,
                200,
                {
                    "ok": True,
                    "mode": updated,
                    "max_mode": max_mode,
                    "workspace": workspace,
                    "can_bypass": can_bypass,
                },
                origin_header,
            )

        if method == "POST" and path == "/v1/client-agent/execute":
            body, body_status = _read_json_body(environ)
            if body is None:
                return _respond(
                    start_response,
                    body_status,
                    _body_error_payload(body_status),
                    origin_header,
                )
            raw_tool = body.get("tool")
            if not isinstance(raw_tool, str) or len(raw_tool) > _MAX_TOOL_NAME_CHARS or _has_control_chars(raw_tool):
                return _respond(
                    start_response,
                    400,
                    {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "tool must be a non-empty string"}},
                    origin_header,
                )
            tool = raw_tool.strip()
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
            raw_approved = body.get("approved")
            if raw_approved is not None and not isinstance(raw_approved, bool):
                return _respond(
                    start_response,
                    400,
                    {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "approved must be a boolean"}},
                    origin_header,
                )
            approved = raw_approved is True
            raw_call_id = body.get("call_id")
            if raw_call_id is not None:
                if (
                    not isinstance(raw_call_id, str)
                    or len(raw_call_id) > _MAX_CALL_ID_CHARS
                    or _has_control_chars(raw_call_id)
                ):
                    return _respond(
                        start_response,
                        400,
                        {
                            "ok": False,
                            "error": {
                                "code": "INVALID_REQUEST",
                                "message": f"call_id must be a string of at most {_MAX_CALL_ID_CHARS} characters",
                            },
                        },
                        origin_header,
                    )
                call_id = raw_call_id.strip()
            else:
                call_id = ""
            raw_approval_token = body.get("approval_token")
            if raw_approval_token is not None:
                if (
                    not isinstance(raw_approval_token, str)
                    or len(raw_approval_token) > _MAX_APPROVAL_TOKEN_CHARS
                    or _has_control_chars(raw_approval_token)
                ):
                    return _respond(
                        start_response,
                        400,
                        {
                            "ok": False,
                            "error": {"code": "INVALID_REQUEST", "message": "approval_token is invalid"},
                        },
                        origin_header,
                    )
                approval_token = raw_approval_token.strip()
            else:
                approval_token = ""
            has_supplied_digest = "args_digest" in body and body.get("args_digest") is not None
            raw_supplied_digest = body.get("args_digest")
            if has_supplied_digest:
                if (
                    not isinstance(raw_supplied_digest, str)
                    or len(raw_supplied_digest) > 128
                    or _has_control_chars(raw_supplied_digest)
                ):
                    return _respond(
                        start_response,
                        400,
                        {
                            "ok": False,
                            "error": {"code": "INVALID_REQUEST", "message": "args_digest must be a string"},
                        },
                        origin_header,
                    )
                supplied_args_digest = raw_supplied_digest.strip().lower()
            else:
                supplied_args_digest = ""
            raw_mode = body.get("mode")
            if raw_mode is not None:
                if not isinstance(raw_mode, str) or len(raw_mode) > _MAX_MODE_CHARS or _has_control_chars(raw_mode):
                    return _respond(
                        start_response,
                        400,
                        {"ok": False, "error": {"code": "INVALID_MODE", "message": "mode must be a valid string"}},
                        origin_header,
                    )
                requested_mode = raw_mode.strip()
                if requested_mode and requested_mode not in _MODE_RANK:
                    return _respond(
                        start_response,
                        400,
                        {"ok": False, "error": {"code": "INVALID_MODE", "message": "Unknown permission mode"}},
                        origin_header,
                    )
            else:
                requested_mode = ""
            clean_tool = tool.lstrip("/")
            try:
                args_digest = canonical_args_digest(raw_args)
            except (TypeError, ValueError, RecursionError):
                return _respond(
                    start_response,
                    400,
                    {"ok": False, "error": {"code": "INVALID_REQUEST", "message": "args cannot be serialized"}},
                    origin_header,
                )
            with lock:
                effective_policy = ClientAgentPolicy(
                    workspace_root=policy.workspace_root,
                    mode=policy.mode,
                    max_mode=policy.max_mode,
                    can_bypass=policy.can_bypass,
                )
                # Caller may request a stricter mode per call, never a looser one.
                if (
                    requested_mode in _MODE_RANK
                    and _MODE_RANK[requested_mode] < _MODE_RANK[policy.mode]
                ):
                    effective_policy.mode = requested_mode
                tool_known = clean_tool in CLIENT_DELEGATABLE_TOOLS
                risk = tool_risk_of(clean_tool) if tool_known else "execute"
                plan_denied = tool_known and effective_policy.mode == "plan" and risk != "read_only"
                requires_approval = (
                    tool_known
                    and not plan_denied
                    and not mode_allows_without_prompt(effective_policy.mode, risk)
                )
                verified_approval = False
                challenge_payload: dict[str, Any] | None = None
                if requires_approval:
                    if approved and approval_token:
                        ok_challenge, reason, tampered = _consume_challenge_locked(
                            approval_token,
                            call_id=call_id,
                            clean_tool=clean_tool,
                            args_digest=args_digest,
                            has_supplied_digest=has_supplied_digest,
                            supplied_args_digest=supplied_args_digest,
                        )
                        if ok_challenge:
                            verified_approval = True
                        elif tampered:
                            challenge_payload = {
                                "ok": False,
                                "output": "",
                                "error": reason,
                                "denied": True,
                                "needs_approval": False,
                                "mode": effective_policy.mode,
                                "risk": risk,
                            }
                        else:
                            fresh = _mint_challenge_locked(call_id, clean_tool, args_digest)
                            challenge_payload = {
                                "ok": False,
                                "output": "",
                                "error": reason,
                                "denied": False,
                                "needs_approval": True,
                                "mode": effective_policy.mode,
                                "risk": risk,
                                "approval_token": fresh.token,
                                "args_digest": fresh.args_digest,
                            }
                    else:
                        fresh = _mint_challenge_locked(call_id, clean_tool, args_digest)
                        challenge_payload = {
                            "ok": False,
                            "output": "",
                            "error": (
                                f"approval required on client PC for '{clean_tool}' "
                                f"({risk}) in {effective_policy.mode} mode"
                            ),
                            "denied": False,
                            "needs_approval": True,
                            "mode": effective_policy.mode,
                            "risk": risk,
                            "approval_token": fresh.token,
                            "args_digest": fresh.args_digest,
                        }
            if challenge_payload is not None:
                if call_id:
                    challenge_payload["call_id"] = call_id
                return _respond(start_response, 200, challenge_payload, origin_header)
            result = execute_client_tool(
                tool,
                raw_args,
                policy=effective_policy,
                approved=verified_approval,
            )
            payload = result.to_dict()
            secrets_bag = (token, approval_token) if approval_token else (token,)
            if isinstance(payload.get("error"), str) and payload["error"]:
                payload["error"] = _sanitize_error_message(
                    payload["error"],
                    secrets_to_redact=secrets_bag,
                )
            if isinstance(payload.get("output"), str) and payload["output"] and len(token) >= 6:
                payload["output"] = payload["output"].replace(token, "[redacted]")
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
        safe_path = _sanitize_error_message(path, secrets_to_redact=(token,), limit=_MAX_REFLECTED_PATH_CHARS)
        return _respond(
            start_response,
            404,
            {"ok": False, "error": {"code": "NOT_FOUND", "message": f"Unknown path: {safe_path}"}},
            origin_header,
        )

    def app(environ: WSGIEnvironment, start_response: StartResponse) -> Iterable[bytes]:
        try:
            return _handle_request(environ, start_response)
        except Exception:  # noqa: BLE001 - fail closed with zero traceback/secret leak
            return _respond(
                start_response,
                500,
                {"ok": False, "error": {"code": "INTERNAL_ERROR", "message": "Internal server error"}},
            )

    return app


def create_client_agent_server(
    *,
    host: str = DEFAULT_CLIENT_AGENT_HOST,
    port: int = DEFAULT_CLIENT_AGENT_PORT,
    workspace_root: Path | str | None = None,
    mode: str = "manual",
    max_mode: str | None = None,
    allow_bypass: bool = False,
    token: str | None = None,
    allowed_origins: Iterable[str] = (),
    approval_ttl_seconds: float = _DEFAULT_APPROVAL_TTL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> ClientAgentServer:
    """Create a loopback-bound ``ClientAgentServer`` ready to ``serve_forever()``."""
    clean_host = host.strip() or DEFAULT_CLIENT_AGENT_HOST
    if not is_loopback_host(clean_host):
        raise ValueError(f"client_agent must bind to a loopback address, not {clean_host!r}")
    bind_host = (
        clean_host[1:-1]
        if clean_host.startswith("[") and clean_host.endswith("]")
        else clean_host
    )
    server_cls = _ClientAgentIPv6Server if ":" in bind_host else ClientAgentServer
    effective_token = (token or "").strip() or secrets.token_urlsafe(32)
    if len(effective_token) > _MAX_TOKEN_CHARS or _has_control_chars(effective_token):
        raise ValueError("client_agent token must be a single-line string without control characters")
    policy = ClientAgentPolicy(
        workspace_root=workspace_root,
        mode=mode,
        max_mode=max_mode,
        can_bypass=allow_bypass,
    )
    normalized_origins = frozenset(
        norm for raw in allowed_origins if (norm := normalize_origin(raw)) is not None
    )
    app = build_wsgi_app(
        policy=policy,
        token=effective_token,
        allowed_origins=normalized_origins,
        approval_ttl_seconds=approval_ttl_seconds,
        clock=clock,
    )
    srv = make_server(
        bind_host,
        int(port),
        app,
        server_class=server_cls,
        handler_class=_QuietWSGIHandler,
    )
    # Attach metadata directly on the server instance for callers/tests.
    if isinstance(srv, ClientAgentServer):
        srv.token = effective_token
        srv.policy = policy
        srv.allowed_origins = normalized_origins
        return srv
    raise RuntimeError("unexpected WSGI server class")
