import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

import local_service_probe as probe


def _addr(address, port=8080):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    sockaddr = (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
    return (family, socket.SOCK_STREAM, 6, "", sockaddr)


@pytest.mark.parametrize(
    "url",
    [
        "http://8.8.8.8:80/",
        "http://192.168.1.1:80/",
        "http://2130706433:80/",
        "http://0x7f000001:80/",
        "http://0177.0.0.1:80/",
        "http://[::ffff:127.0.0.1]:80/",
        "http://user:pass@127.0.0.1:80/",
        "http://127.0.0.1:80/#fragment",
        "http://127.0.0.1/",
        "file:///etc/passwd",
    ],
)
def test_target_rejects_ssrf_and_ambiguous_url_forms(url):
    with pytest.raises(ValueError):
        probe._validate_target(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:8080/.git/config",
        "http://127.0.0.1:8080/.git%252fconfig",
        "http://127.0.0.1:8080/.env",
        "http://127.0.0.1:8080/.env.local",
        "http://127.0.0.1:8080/id_rsa.pem",
        "http://127.0.0.1:8080/permissions.json",
        "http://127.0.0.1:8080/health?token=secret",
        "http://127.0.0.1:8080/health?api-key=secret",
        "http://127.0.0.1:8080/health?api%252dkey=secret",
    ],
)
def test_target_rejects_control_state_and_credential_parameters(url):
    with pytest.raises(ValueError, match="control-plane|credential"):
        probe._validate_target(url)


def test_dns_name_must_resolve_exclusively_to_loopback(monkeypatch):
    monkeypatch.setattr(
        probe.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [_addr("127.0.0.1"), _addr("8.8.8.8")],
    )
    with pytest.raises(ValueError, match="exclusively"):
        probe._validate_target("http://dev.local:8080/health")


def test_dns_is_rechecked_and_rebinding_is_refused_before_socket(monkeypatch):
    answers = [
        [_addr("127.0.0.1")],
        [_addr("8.8.8.8")],
    ]
    monkeypatch.setattr(
        probe.socket, "getaddrinfo", lambda *args, **kwargs: answers.pop(0),
    )
    parsed, approved = probe._validate_target("http://dev.local:8080/health")
    with pytest.raises(ValueError, match="exclusively"):
        probe._connect_pinned(
            parsed.hostname, parsed.port, approved, probe.time.monotonic() + 1.0,
        )
    assert answers == []


class _FakeSocket:
    def __init__(self, response):
        self.response = bytearray(response)
        self.connected = None
        self.timeout = None
        self.sent = b""
        self.closed = False

    def settimeout(self, value):
        self.timeout = value

    def connect(self, target):
        self.connected = target

    def sendall(self, data):
        self.sent += data

    def recv(self, size):
        data = bytes(self.response[:size])
        del self.response[:size]
        return data

    def close(self):
        self.closed = True


def test_direct_transport_ignores_proxy_env_and_sends_no_auth_or_cookies(
    monkeypatch,
):
    fake = _FakeSocket(
        b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\nok"
    )
    monkeypatch.setenv("HTTP_PROXY", "http://attacker.example:9999")
    monkeypatch.setenv("HTTPS_PROXY", "http://attacker.example:9999")
    monkeypatch.setattr(probe.socket, "socket", lambda *args, **kwargs: fake)
    parsed, addresses = probe._validate_target("http://127.0.0.1:8080/health")

    status, headers, body, truncated = probe._request_once(
        parsed, addresses, "GET", 1.25,
    )

    assert (status, headers["content-type"], body, truncated) == (
        200, "text/plain", b"ok", False,
    )
    assert fake.connected == ("127.0.0.1", 8080)
    assert 0 < fake.timeout <= 1.25
    lowered = fake.sent.lower()
    assert b"proxy" not in lowered
    assert b"authorization" not in lowered
    assert b"cookie" not in lowered


def test_header_and_body_caps_are_enforced(monkeypatch):
    oversized_headers = _FakeSocket(
        b"HTTP/1.1 200 OK\r\nX-Large: "
        + b"a" * probe.MAX_HEADER_BYTES
        + b"\r\n\r\n"
    )
    monkeypatch.setattr(
        probe.socket, "socket", lambda *args, **kwargs: oversized_headers,
    )
    parsed, addresses = probe._validate_target("http://127.0.0.1:8080/")
    with pytest.raises(ValueError, match="headers exceed"):
        probe._request_once(parsed, addresses, "GET", 1.0)

    payload = b"x" * (probe.MAX_BODY_BYTES + 500)
    oversized_body = _FakeSocket(
        b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(payload)
        + payload
    )
    monkeypatch.setattr(
        probe.socket, "socket", lambda *args, **kwargs: oversized_body,
    )
    _status, _headers, body, truncated = probe._request_once(
        parsed, addresses, "GET", 1.0,
    )
    assert len(body) == probe.MAX_BODY_BYTES
    assert truncated is True


def test_chunked_body_is_bounded_and_supported(monkeypatch):
    fake = _FakeSocket(
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n"
        b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n"
    )
    monkeypatch.setattr(probe.socket, "socket", lambda *args, **kwargs: fake)
    parsed, addresses = probe._validate_target("http://127.0.0.1:8080/")
    _status, _headers, body, truncated = probe._request_once(
        parsed, addresses, "GET", 1.0,
    )
    assert body == b"hello world"
    assert truncated is False


def test_incomplete_content_length_is_rejected(monkeypatch):
    fake = _FakeSocket(
        b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nshort"
    )
    monkeypatch.setattr(probe.socket, "socket", lambda *args, **kwargs: fake)
    parsed, addresses = probe._validate_target("http://127.0.0.1:8080/")
    with pytest.raises(ValueError, match="completing Content-Length"):
        probe._request_once(parsed, addresses, "GET", 1.0)


def test_redirect_to_non_loopback_is_rejected(monkeypatch):
    monkeypatch.setattr(
        probe,
        "_request_once",
        lambda *args, **kwargs: (
            302, {"location": "http://169.254.169.254:80/latest/meta-data"},
            b"", False,
        ),
    )
    with pytest.raises(ValueError, match="loopback"):
        probe.probe("http://127.0.0.1:8080/redirect")


def test_loopback_redirect_and_secret_preview_are_safe_and_deterministic(monkeypatch):
    responses = [
        (302, {"location": "/health"}, b"", False),
        (
            200,
            {"content-type": "application/json; charset=utf-8"},
            b'{"token":"super-secret-value","ok":true}',
            False,
        ),
    ]
    deadlines = []

    def request(*_args, **kwargs):
        deadlines.append(kwargs["deadline"])
        return responses.pop(0)

    monkeypatch.setattr(probe, "_request_once", request)
    monkeypatch.setattr(probe.time, "monotonic", lambda: 10.0)

    result = probe.probe("http://127.0.0.1:8080/start")

    assert result == {
        "body_bytes": 40,
        "body_preview": '{"token":[REDACTED],"ok":true}',
        "body_truncated": False,
        "content_type": "application/json",
        "final_url": "http://127.0.0.1:8080/health",
        "latency_ms": 0,
        "method": "GET",
        "redirects": 1,
        "status": 200,
    }
    assert deadlines == [12.0, 12.0]


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "OPTIONS"])
def test_only_get_and_head_are_allowed(method):
    with pytest.raises(ValueError, match="GET or HEAD"):
        probe.probe("http://127.0.0.1:8080/", method=method)


def test_timeout_is_strictly_bounded():
    with pytest.raises(ValueError, match="at most"):
        probe.probe("http://127.0.0.1:8080/", timeout=probe.MAX_TIMEOUT_SECONDS + 0.1)


def test_socket_timeout_is_propagated_as_a_bounded_failure(monkeypatch):
    class TimeoutSocket(_FakeSocket):
        def recv(self, _size):
            raise socket.timeout("timed out")

    fake = TimeoutSocket(b"")
    monkeypatch.setattr(probe.socket, "socket", lambda *args, **kwargs: fake)
    parsed, addresses = probe._validate_target("http://127.0.0.1:8080/")
    with pytest.raises(socket.timeout, match="timed out"):
        probe._request_once(parsed, addresses, "GET", 0.25)
    assert 0 < fake.timeout <= 0.25
    assert fake.closed is True


@pytest.mark.parametrize("slow_phase", ["headers", "body"])
def test_absolute_deadline_stops_slow_drip_responses(monkeypatch, slow_phase):
    clock = {"now": 100.0}
    headers = b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\n\r\n"

    class SlowDripSocket(_FakeSocket):
        def __init__(self):
            super().__init__(headers + b"x" * 20)
            self.recv_calls = 0
            self.timeouts = []

        def settimeout(self, value):
            super().settimeout(value)
            self.timeouts.append(value)

        def recv(self, size):
            self.recv_calls += 1
            if slow_phase == "body" and self.recv_calls == 1:
                data = bytes(self.response[:len(headers)])
                del self.response[:len(headers)]
            else:
                data = bytes(self.response[:1])
                del self.response[:1]
            # Simulate a peer that always supplies a byte before the current
            # socket timeout but consumes the shared wall-clock budget.
            clock["now"] += 0.26
            return data

    fake = SlowDripSocket()
    monkeypatch.setattr(probe.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(probe.socket, "socket", lambda *args, **kwargs: fake)
    parsed, addresses = probe._validate_target("http://127.0.0.1:8080/")

    with pytest.raises(TimeoutError, match="total timeout"):
        probe._request_once(parsed, addresses, "GET", 1.0)

    assert fake.closed is True
    assert fake.recv_calls <= 4
    assert all(
        later <= earlier
        for earlier, later in zip(fake.timeouts, fake.timeouts[1:])
    )
    assert fake.timeouts[-1] < fake.timeouts[0]


def test_real_loopback_http_probe_end_to_end():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            payload = b'{"ready":true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            pass

    service = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=service.serve_forever, daemon=True)
    thread.start()
    try:
        result = probe.probe(
            "http://127.0.0.1:%d/health" % service.server_address[1],
            timeout=1.0,
        )
    finally:
        service.shutdown()
        service.server_close()
        thread.join(timeout=2)
    assert result["status"] == 200
    assert result["content_type"] == "application/json"
    assert result["body_preview"] == '{"ready":true}'
    assert result["body_truncated"] is False
