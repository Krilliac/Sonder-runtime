"""Unread-request-body framing on the HTTP adapter.

Several ``/v1`` routes answer before ``_read_json`` takes the request body off
the socket: the origin rejection, the framing and media-type errors, the
oversized-body refusal, the authentication-failure limiter, and
``/v1/admin/drain``.  ``Handler.protocol_version`` defaults to ``HTTP/1.0`` so
those connections close anyway, but the deployment may opt into HTTP/1.1
(``test_chat_keepalive_requests_receive_distinct_correlation_ids`` in
``test_serve_auth.py`` exercises exactly that).  On such a connection the
skipped bytes are parsed as the *next* request line, so the caller's following
request is silently dropped and replaced by a forged one -- the classic
response-queue desync that RFC 9112 s6.3 requires a server to prevent by
closing the connection whenever it does not consume the body.
"""

from contextlib import contextmanager
import http.client
import io
import socket
import threading
import time

import pytest

import sonder_runtime.interfaces.http.serve as ts


CHAT_BODY = (
    b'{"model":"sonder","messages":[{"role":"user","content":"hello"}]}'
)


class _FakeMetric:
    def labels(self, **_kwargs):
        return self

    def inc(self, *_args, **_kwargs):
        return None

    def observe(self, *_args, **_kwargs):
        return None


class _FakeMetrics:
    requests_total = _FakeMetric()
    request_duration_seconds = _FakeMetric()


class _FakeLifecycle:
    """Enough lifecycle surface for the routes exercised here."""

    metrics = _FakeMetrics()

    def idempotent(self, _key, factory):
        return factory()

    def auth_attempt_allowed(self, _peer):
        return True

    def record_auth_failure(self, *_args, **_kwargs):
        return None

    def drain(self, *_args, **_kwargs):
        return None

    def live_payload(self):
        return {"status": "alive"}

    def error_envelope(self, code, message, correlation_id, retryable=False):
        return {"error": {
            "code": code, "message": message,
            "correlation_id": correlation_id, "retryable": retryable,
        }}


@contextmanager
def _keepalive_server(monkeypatch):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts.Handler, "protocol_version", "HTTP/1.1")
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts.sonder_lifecycle, "get", lambda: _FakeLifecycle())
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _pipeline(port, first_request):
    """Send ``first_request``, then a follow-up on the same connection."""
    follow_up = (
        b"GET /live HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=5) as sock:
        sock.sendall(first_request)
        # Let the server answer the first request before the follow-up lands,
        # so a desync is attributable to the unread body rather than to
        # interleaved writes.
        time.sleep(0.3)
        try:
            sock.sendall(follow_up)
        except OSError:
            # The server may already have closed the connection, which is the
            # correct outcome; the response collected below is what matters.
            pass
        sock.settimeout(5)
        received = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                received += chunk
        except OSError:
            pass
    return received


ALIVE = b'"status": "alive"'


def _assert_no_body_desync(received):
    """The skipped body must never be parsed as the start of a request."""
    lowered = received.lower()
    assert b"unsupported method" not in lowered, received[:400]
    assert b"bad request syntax" not in lowered, received[:400]
    # The stdlib error page is only reachable through send_error, which this
    # adapter never uses; seeing it means a forged request line was routed.
    assert b"<!doctype html>" not in lowered, received[:400]


def _assert_body_discarded(received):
    """The framed body was taken off the socket, so the connection is reusable."""
    _assert_no_body_desync(received)
    assert received.count(b"HTTP/1.1 ") == 2, received[:400]
    assert ALIVE in received, received[:400]


def _assert_connection_ended(received):
    """The body was left unread, so the follow-up must not be answered here.

    Only the desync invariant is asserted on the first response: closing a
    socket that still holds unread bytes can reset the peer, and a truncated
    error response is an acceptable outcome where a forged request is not.
    """
    _assert_no_body_desync(received)
    assert ALIVE not in received, received[:400]


def _post(path, body, content_type=b"application/json", declared_length=None):
    length = len(body) if declared_length is None else declared_length
    return (
        b"POST " + path + b" HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: " + content_type + b"\r\n"
        b"Content-Length: " + str(length).encode() + b"\r\n"
        b"\r\n" + body
    )


def test_admin_drain_body_is_not_parsed_as_the_next_request(monkeypatch):
    """``/v1/admin/drain`` answers before ``_read_json`` and skips its body."""
    with _keepalive_server(monkeypatch) as port:
        received = _pipeline(
            port, _post(b"/v1/admin/drain", b'{"reason":"maintenance"}')
        )

    assert received.startswith(b"HTTP/1.1 202"), received[:400]
    _assert_body_discarded(received)


def test_unsupported_media_type_does_not_leak_its_body(monkeypatch):
    with _keepalive_server(monkeypatch) as port:
        received = _pipeline(
            port,
            _post(b"/v1/chat/completions", CHAT_BODY, content_type=b"text/plain"),
        )

    assert received.startswith(b"HTTP/1.1 415"), received[:400]
    _assert_body_discarded(received)


def test_oversized_body_refusal_does_not_leak_its_body(monkeypatch):
    """The 413 deliberately never reads the body, so it must close."""
    with _keepalive_server(monkeypatch) as port:
        received = _pipeline(
            port,
            _post(
                b"/v1/chat/completions",
                b"x" * 4096,
                declared_length=ts.MAX_REQUEST_BYTES + 1,
            ),
        )

    # The lingering close keeps the refusal readable: without it the reset
    # from closing on an unread body destroys the response the caller needs.
    assert received.startswith(b"HTTP/1.1 413"), received[:400]
    assert b"connection: close" in received.lower(), received[:400]
    _assert_connection_ended(received)


def test_rejected_transfer_coding_does_not_leak_its_body(monkeypatch):
    """Rejected framing means the body boundary is unknown; close the socket."""
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Transfer-Encoding: chunked\r\n"
        b"\r\n"
        b"5\r\nhello\r\n0\r\n\r\n"
    )
    with _keepalive_server(monkeypatch) as port:
        received = _pipeline(port, request)

    assert received.startswith(b"HTTP/1.1 400"), received[:400]
    assert b"transfer encoding is not supported" in received, received[:400]
    _assert_connection_ended(received)


def test_unauthenticated_chat_request_does_not_leak_its_body(monkeypatch):
    """Chat rejects credentials only after ``_read_json``; keep it that way.

    Moving the authentication check above the body read would look like a
    saving and would reintroduce the desync on the single most-hit route, so
    pin the observable outcome rather than the ordering.
    """
    monkeypatch.setattr(
        ts.Handler, "_request_auth_context",
        lambda _self: {
            "mode": "api-key", "authorized": False, "api_key": False,
            "account": None,
        },
    )
    with _keepalive_server(monkeypatch) as port:
        received = _pipeline(port, _post(b"/v1/chat/completions", CHAT_BODY))

    assert received.startswith(b"HTTP/1.1 401"), received[:400]
    _assert_body_discarded(received)


def test_disallowed_origin_rejection_does_not_leak_its_body(monkeypatch):
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Origin: https://evil.example\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: " + str(len(CHAT_BODY)).encode() + b"\r\n"
        b"\r\n" + CHAT_BODY
    )
    with _keepalive_server(monkeypatch) as port:
        monkeypatch.setattr(ts, "CORS_ORIGINS", frozenset({"https://ok.example"}))
        received = _pipeline(port, request)

    assert received.startswith(b"HTTP/1.1 403"), received[:400]
    _assert_body_discarded(received)


def test_consumed_body_keeps_the_connection_reusable(monkeypatch):
    """Guard against over-closing: a fully read body must not end the socket.

    Malformed JSON is rejected only after ``_read_json`` has taken the whole
    declared body off the socket, so the next pipelined request is still
    unambiguous and must be answered normally.
    """
    with _keepalive_server(monkeypatch) as port:
        received = _pipeline(port, _post(b"/v1/chat/completions", b"not-json"))

    assert received.startswith(b"HTTP/1.1 400"), received[:400]
    assert b"valid JSON" in received, received[:400]
    # The follow-up /live request was answered on the same connection.
    assert received.count(b"HTTP/1.1 ") == 2, received[:400]
    assert b'"status": "alive"' in received, received[:400]


def test_bodyless_post_keeps_the_connection_reusable(monkeypatch):
    """Nothing was skipped when no body was declared, so do not close.

    ``/v1/admin/drain`` is the one POST route that answers without reading a
    body at all, which makes it the honest test of the "nothing pending"
    branch rather than of the discard path above.
    """
    request = (
        b"POST /v1/admin/drain HTTP/1.1\r\n"
        b"Host: 127.0.0.1\r\n"
        b"Content-Type: application/json\r\n"
        b"Content-Length: 0\r\n"
        b"\r\n"
    )
    with _keepalive_server(monkeypatch) as port:
        received = _pipeline(port, request)

    assert received.startswith(b"HTTP/1.1 202"), received[:400]
    assert b"connection: close" not in received.lower(), received[:400]
    _assert_body_discarded(received)


@pytest.mark.parametrize(
    ("headers", "consumed", "expected"),
    [
        ({}, False, 0),
        ({"Content-Length": "0"}, False, 0),
        ({"Content-Length": "12"}, False, 12),
        ({"Content-Length": "12"}, True, 0),
        ({"Content-Length": "not-a-number"}, False, None),
        ({"Content-Length": "\u00b2"}, False, None),
        ({"Content-Length": "9" * 5000}, False, None),
        ({"Transfer-Encoding": "chunked"}, False, None),
    ],
)
def test_unread_request_body_bytes_reports_the_skipped_span(
    headers, consumed, expected,
):
    """``None`` means the body boundary cannot be located at all."""
    handler = ts.Handler.__new__(ts.Handler)
    handler.headers = http.client.HTTPMessage()
    for name, value in headers.items():
        handler.headers[name] = value
    handler._request_body_consumed = consumed

    assert handler._unread_request_body_bytes() == expected


CONNECTION_TIMEOUT = 60


class _FakeConnection:
    def __init__(self, timeout=CONNECTION_TIMEOUT):
        self._timeout = timeout
        self.applied = []

    def gettimeout(self):
        return self._timeout

    def settimeout(self, value):
        self._timeout = value
        self.applied.append(value)


def _settling_handler(declared, available):
    handler = ts.Handler.__new__(ts.Handler)
    handler.headers = http.client.HTTPMessage()
    handler.headers["Content-Length"] = str(declared)
    handler._request_body_consumed = False
    handler.rfile = io.BytesIO(available)
    handler.connection = _FakeConnection()
    return handler


def test_oversized_unread_body_is_closed_rather_than_read():
    """The discard bound is what keeps the 413 refusal meaningful.

    Reading the body of a request that was rejected precisely because it is
    too large would hand the caller the resource the limit exists to deny.
    """
    declared = ts.MAX_DISCARDED_BODY_BYTES + 1
    handler = _settling_handler(declared, b"x" * declared)

    assert handler._settle_unread_request_body() is True
    assert handler.rfile.tell() == 0
    assert handler._request_body_consumed is False
    assert handler.connection.applied == []


def test_bounded_unread_body_is_discarded_under_its_own_deadline():
    body = b"x" * 2048
    handler = _settling_handler(len(body), body)

    assert handler._settle_unread_request_body() is False
    assert handler.rfile.tell() == len(body)
    assert handler._request_body_consumed is True
    # Scoped to the discard, then handed back to the per-connection wait.
    assert handler.connection.applied == [
        ts.DISCARD_BODY_TIMEOUT_SECONDS, CONNECTION_TIMEOUT,
    ]


def test_truncated_unread_body_closes_the_connection():
    """A peer that stopped mid-body left the next boundary unknowable."""
    handler = _settling_handler(2048, b"x" * 10)

    assert handler._settle_unread_request_body() is True
    assert handler._request_body_consumed is False


def test_discard_bound_never_exceeds_the_accepted_request_size():
    assert 0 < ts.MAX_DISCARDED_BODY_BYTES <= ts.MAX_REQUEST_BYTES


class _RecordingSocket(_FakeConnection):
    def __init__(self, pending=b""):
        super().__init__()
        self._pending = pending
        self.reads = 0

    def recv(self, size):
        self.reads += 1
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk


def _linger_handler(consumed, close_connection, pending=b"body"):
    handler = ts.Handler.__new__(ts.Handler)
    handler.close_connection = close_connection
    handler.connection = _RecordingSocket(pending)
    handler.wfile = io.BytesIO()
    if consumed is not None:
        handler._request_body_consumed = consumed
    return handler


def test_lingering_close_soaks_only_an_abandoned_body():
    handler = _linger_handler(consumed=False, close_connection=True)

    handler._linger_before_close()

    assert handler.connection.reads > 0
    assert handler.connection.applied == [ts.LINGERING_CLOSE_SECONDS]


@pytest.mark.parametrize(
    ("consumed", "close_connection"),
    [
        (True, True),      # body already read: nothing is in flight
        (False, False),    # connection is being reused, so it was settled
        (None, True),      # answered by the stdlib before any route ran
    ],
)
def test_lingering_close_is_skipped_for_ordinary_connections(
    consumed, close_connection,
):
    handler = _linger_handler(consumed, close_connection)

    handler._linger_before_close()

    assert handler.connection.reads == 0
    assert handler.connection.applied == []
