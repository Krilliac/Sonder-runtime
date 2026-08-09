"""Bounded, read-only HTTP probes for explicitly local development services.

The transport deliberately bypasses urllib and environment proxy handling. A
hostname is resolved twice, every answer must be loopback, and the socket is
connected to a validated numeric address so DNS is never consulted by connect.
"""
from __future__ import annotations

import ipaddress
import re
import socket
import ssl
import time
import urllib.parse

from sonder_logging import Redactor


MAX_TIMEOUT_SECONDS = 5.0
MAX_RESPONSE_BYTES = 80 * 1024
MAX_HEADER_BYTES = 16 * 1024
MAX_BODY_BYTES = MAX_RESPONSE_BYTES - MAX_HEADER_BYTES
MAX_PREVIEW_CHARS = 4096
MAX_REDIRECTS = 3
_REDIRECT_CODES = {301, 302, 303, 307, 308}
_NUMERIC_HOST_PART = re.compile(r"^(?:0[xX][0-9a-fA-F]+|[0-9]+)$")
_SENSITIVE_QUERY_WORDS = {
    "api_key", "apikey", "auth", "authorization", "credential", "key",
    "password", "secret", "token",
}
_SENSITIVE_PATH_NAMES = {
    ".aws", ".azure", ".credentials.json", ".env", ".git", ".kube",
    ".ssh", "auth.json", "credentials.json", "file_roots.local", "memory.db",
    "memory.db-shm", "memory.db-wal", "permissions.json", "secrets.json",
    "token.json",
}
_REDACTOR = Redactor()


def _fully_unquote(value: str) -> str:
    current = value
    for _ in range(3):
        decoded = urllib.parse.unquote(current)
        if decoded == current:
            return decoded
        current = decoded
    return current


def _is_sensitive_path_name(name: str) -> bool:
    lowered = name.casefold()
    return (
        lowered in _SENSITIVE_PATH_NAMES
        or lowered.startswith(".env.")
        or lowered.endswith((".key", ".p12", ".pem", ".pfx"))
    )


def _looks_like_noncanonical_numeric_host(host: str) -> bool:
    candidate = (host or "").rstrip(".")
    if not candidate or ":" in candidate:
        return False
    parts = candidate.split(".")
    if any(not part or not _NUMERIC_HOST_PART.fullmatch(part) for part in parts):
        return False
    try:
        ipaddress.IPv4Address(candidate)
    except ValueError:
        return True
    return False


def _is_allowed_loopback(address: str) -> bool:
    ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    if getattr(ip, "ipv4_mapped", None) is not None:
        return False
    return ip.is_loopback and (ip.version == 4 or ip == ipaddress.IPv6Address("::1"))


def _resolve_loopback_addresses(host: str, port: int) -> tuple[str, ...]:
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        try:
            rows = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError) as exc:
            raise ValueError("probe hostname could not be resolved") from exc
        addresses = set()
        for row in rows:
            sockaddr = row[4]
            if not sockaddr:
                continue
            raw = str(sockaddr[0]).split("%", 1)[0]
            try:
                if not _is_allowed_loopback(raw):
                    raise ValueError(
                        "probe hostname must resolve exclusively to loopback addresses"
                    )
                addresses.add(ipaddress.ip_address(raw).compressed)
            except ValueError as exc:
                if "exclusively" in str(exc):
                    raise
                raise ValueError("probe hostname resolved to an invalid address") from exc
        if not addresses:
            raise ValueError("probe hostname did not resolve to an address")
        return tuple(sorted(addresses))
    if not _is_allowed_loopback(literal.compressed):
        raise ValueError("probe target must be loopback (127/8 or ::1)")
    return (literal.compressed,)


def _validate_target(url: str) -> tuple[urllib.parse.ParseResult, tuple[str, ...]]:
    if not isinstance(url, str) or not url.strip() or len(url) > 2048:
        raise ValueError("url must be an explicit bounded HTTP/HTTPS URL")
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("only HTTP/HTTPS probes are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("URL credentials are not allowed")
    if parsed.fragment:
        raise ValueError("URL fragments are not allowed")
    host = (parsed.hostname or "").strip().lower()
    if not host or "%" in host:
        raise ValueError("URL must contain an unscoped hostname")
    if _looks_like_noncanonical_numeric_host(host):
        raise ValueError("non-canonical numeric IP hostnames are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("URL has an invalid port") from exc
    if port is None:
        raise ValueError("URL must include an explicit port")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in url):
        raise ValueError("URL contains control characters")
    decoded_path = _fully_unquote(parsed.path or "/").replace("\\", "/")
    path_names = {part.casefold() for part in decoded_path.split("/") if part}
    if any(_is_sensitive_path_name(name) for name in path_names):
        raise ValueError("probe URL targets sensitive control-plane state")
    for key, _value in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
        normalized = re.sub(
            r"[^a-z0-9]+", "_", _fully_unquote(key).casefold(),
        ).strip("_")
        if normalized in _SENSITIVE_QUERY_WORDS or any(
            word in normalized.split("_") for word in _SENSITIVE_QUERY_WORDS
        ):
            raise ValueError("probe URL may not contain credential query parameters")
    try:
        dns_host = host.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ValueError("URL has an invalid hostname") from exc
    return parsed, _resolve_loopback_addresses(dns_host, port)


def _host_header(host: str, port: int) -> str:
    display = "[%s]" % host if ":" in host else host
    return "%s:%d" % (display, port)


def _connect_pinned(
    host: str,
    port: int,
    approved_addresses: tuple[str, ...],
    deadline: float,
):
    current = _resolve_loopback_addresses(host, port)
    _set_deadline_timeout(None, deadline)
    candidates = tuple(sorted(set(approved_addresses) & set(current)))
    if not candidates:
        raise ValueError("probe hostname addresses changed before connect")
    last_error = None
    for address in candidates:
        ip = ipaddress.ip_address(address)
        family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
        sock = socket.socket(family, socket.SOCK_STREAM)
        try:
            _set_deadline_timeout(sock, deadline)
            target = (address, port, 0, 0) if ip.version == 6 else (address, port)
            sock.connect(target)
            _set_deadline_timeout(None, deadline)
            return sock
        except OSError as exc:
            last_error = exc
            sock.close()
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    "local service probe exceeded its total timeout"
                ) from exc
    raise OSError("no validated loopback address was reachable") from last_error


def _set_deadline_timeout(sock, deadline: float) -> float:
    """Apply the remaining monotonic budget before one blocking operation."""
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("local service probe exceeded its total timeout")
    if sock is not None:
        sock.settimeout(remaining)
    return remaining


def _recv(sock, size: int, deadline: float) -> bytes:
    _set_deadline_timeout(sock, deadline)
    chunk = sock.recv(size)
    _set_deadline_timeout(None, deadline)
    return chunk


def _recv_until(sock, marker: bytes, limit: int, deadline: float) -> tuple[bytes, bytes]:
    data = bytearray()
    while marker not in data:
        chunk = _recv(sock, min(4096, limit + 1 - len(data)), deadline)
        if not chunk:
            raise ValueError("local service closed before completing response headers")
        data.extend(chunk)
        if len(data) > limit:
            raise ValueError("local service response headers exceed %d bytes" % limit)
    head, remainder = bytes(data).split(marker, 1)
    return head, remainder


def _parse_headers(raw: bytes) -> tuple[int, dict[str, str]]:
    lines = raw.split(b"\r\n")
    if not lines or len(lines[0]) > 1024:
        raise ValueError("local service returned an invalid status line")
    match = re.fullmatch(rb"HTTP/1\.[01] ([0-9]{3})(?: [^\r\n]*)?", lines[0])
    if not match:
        raise ValueError("local service returned an invalid HTTP status line")
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line or b":" not in line or line[:1] in b" \t":
            raise ValueError("local service returned malformed response headers")
        name, value = line.split(b":", 1)
        try:
            key = name.decode("ascii").strip().lower()
            text = value.decode("latin-1").strip()
        except UnicodeError as exc:
            raise ValueError("local service returned invalid response headers") from exc
        if not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", key):
            raise ValueError("local service returned an invalid header name")
        if any(ord(character) < 0x20 and character != "\t" for character in text):
            raise ValueError("local service returned an invalid header value")
        headers[key] = "%s, %s" % (headers[key], text) if key in headers else text
    status = int(match.group(1))
    if not 100 <= status <= 599:
        raise ValueError("local service returned an invalid HTTP status")
    return status, headers


def _read_chunked(sock, initial: bytes, deadline: float) -> tuple[bytes, bool]:
    buffered = bytearray(initial)
    body = bytearray()

    def receive_more() -> None:
        chunk = _recv(sock, 4096, deadline)
        if not chunk:
            raise ValueError("local service closed during chunked response")
        buffered.extend(chunk)

    def read_line(limit: int = 128) -> bytes:
        while b"\r\n" not in buffered:
            if len(buffered) > limit:
                raise ValueError("local service returned an oversized chunk line")
            receive_more()
        line, remainder = bytes(buffered).split(b"\r\n", 1)
        if len(line) > limit:
            raise ValueError("local service returned an oversized chunk line")
        buffered[:] = remainder
        return line

    while True:
        raw_size = read_line().split(b";", 1)[0].strip()
        try:
            size = int(raw_size, 16)
        except ValueError as exc:
            raise ValueError("local service returned an invalid chunk size") from exc
        if size < 0:
            raise ValueError("local service returned an invalid chunk size")
        if size == 0:
            return bytes(body), False
        remaining_cap = MAX_BODY_BYTES + 1 - len(body)
        if size > remaining_cap:
            while len(buffered) < remaining_cap:
                receive_more()
            body.extend(buffered[:remaining_cap])
            return bytes(body[:MAX_BODY_BYTES]), True
        while len(buffered) < size + 2:
            receive_more()
        body.extend(buffered[:size])
        if buffered[size:size + 2] != b"\r\n":
            raise ValueError("local service returned a malformed chunk terminator")
        del buffered[:size + 2]
        if len(body) > MAX_BODY_BYTES:
            return bytes(body[:MAX_BODY_BYTES]), True


def _read_body(
    sock, initial: bytes, headers: dict[str, str], method: str, deadline: float,
) -> tuple[bytes, bool]:
    if method == "HEAD":
        return b"", False
    content_encoding = headers.get("content-encoding", "").strip().casefold()
    if content_encoding not in {"", "identity"}:
        raise ValueError("encoded local service response bodies are not supported")
    transfer_encoding = headers.get("transfer-encoding", "").casefold()
    if transfer_encoding == "chunked":
        return _read_chunked(sock, initial, deadline)
    if transfer_encoding and transfer_encoding != "identity":
        raise ValueError("unsupported local service transfer encoding")
    length_text = headers.get("content-length", "")
    if length_text:
        try:
            length = int(length_text)
        except ValueError as exc:
            raise ValueError("local service returned invalid Content-Length") from exc
        if length < 0:
            raise ValueError("local service returned invalid Content-Length")
        wanted = min(length, MAX_BODY_BYTES + 1)
    else:
        wanted = MAX_BODY_BYTES + 1
    data = bytearray(initial[:wanted])
    while len(data) < wanted:
        chunk = _recv(sock, min(4096, wanted - len(data)), deadline)
        if not chunk:
            break
        data.extend(chunk)
    if length_text and len(data) < wanted:
        raise ValueError("local service closed before completing Content-Length body")
    truncated = length > MAX_BODY_BYTES if length_text else len(data) > MAX_BODY_BYTES
    return bytes(data[:MAX_BODY_BYTES]), truncated


def _request_once(
    parsed: urllib.parse.ParseResult,
    addresses: tuple[str, ...],
    method: str,
    timeout: float,
    *,
    deadline: float | None = None,
) -> tuple[int, dict[str, str], bytes, bool]:
    if deadline is None:
        deadline = time.monotonic() + timeout
    host = (parsed.hostname or "").rstrip(".").encode("idna").decode("ascii")
    port = parsed.port
    sock = _connect_pinned(host, port, addresses, deadline)
    try:
        if parsed.scheme == "https":
            context = ssl.create_default_context()
            _set_deadline_timeout(sock, deadline)
            sock = context.wrap_socket(sock, server_hostname=host)
            _set_deadline_timeout(None, deadline)
        path = urllib.parse.quote(
            urllib.parse.unquote(parsed.path or "/"),
            safe="/:@!$&'()*+,;=-._~%",
        )
        query = urllib.parse.quote(
            urllib.parse.unquote(parsed.query),
            safe="!$&'()*+,;=:@/?-._~%",
        )
        target = urllib.parse.urlunsplit(("", "", path, query, ""))
        request = (
            "%s %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: sonder-local-probe/1.0\r\n"
            "Accept: */*\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n"
            % (method, target, _host_header(host, port))
        ).encode("ascii")
        _set_deadline_timeout(sock, deadline)
        sock.sendall(request)
        _set_deadline_timeout(None, deadline)
        raw_headers, initial = _recv_until(
            sock, b"\r\n\r\n", MAX_HEADER_BYTES, deadline,
        )
        status, headers = _parse_headers(raw_headers)
        body, truncated = _read_body(sock, initial, headers, method, deadline)
        _set_deadline_timeout(None, deadline)
        return status, headers, body, truncated
    finally:
        sock.close()


def probe(url: str, *, method: str = "GET", timeout: float = 2.0) -> dict:
    method = str(method or "GET").upper()
    if method not in {"GET", "HEAD"}:
        raise ValueError("local probe method must be GET or HEAD")
    try:
        timeout = float(timeout)
    except (TypeError, ValueError) as exc:
        raise ValueError("timeout must be a number") from exc
    if not 0 < timeout <= MAX_TIMEOUT_SECONDS:
        raise ValueError("timeout must be greater than 0 and at most %.1f seconds" % MAX_TIMEOUT_SECONDS)
    current_url = url
    redirects = 0
    started = time.monotonic()
    deadline = started + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("local service probe exceeded its total timeout")
        parsed, addresses = _validate_target(current_url)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("local service probe exceeded its total timeout")
        status, headers, body, truncated = _request_once(
            parsed, addresses, method, remaining, deadline=deadline,
        )
        if status in _REDIRECT_CODES:
            if redirects >= MAX_REDIRECTS:
                raise ValueError("too many local redirects (max %d)" % MAX_REDIRECTS)
            location = headers.get("location", "")
            if not location:
                raise ValueError("redirect response has no Location header")
            next_url = urllib.parse.urljoin(current_url, location)
            _validate_target(next_url)
            current_url = next_url
            redirects += 1
            continue
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if len(content_type) > 256:
            raise ValueError("local service Content-Type exceeds 256 characters")
        decoded = "" if method == "HEAD" else body.decode("utf-8", errors="replace")
        preview_truncated = len(decoded) > MAX_PREVIEW_CHARS
        preview = _REDACTOR.redact(decoded[:MAX_PREVIEW_CHARS])
        return {
            "body_bytes": len(body),
            "body_preview": preview,
            "body_truncated": bool(truncated or preview_truncated),
            "content_type": content_type,
            "final_url": urllib.parse.urlunsplit((
                parsed.scheme, parsed.netloc, parsed.path or "/", "", "",
            )),
            "latency_ms": max(0, int((time.monotonic() - started) * 1000)),
            "method": method,
            "redirects": redirects,
            "status": status,
        }
