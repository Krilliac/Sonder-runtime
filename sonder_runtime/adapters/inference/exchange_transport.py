"""Direct HTTP exchange primitives shared by the inference gateways.

A leaf module: it imports neither gateway, so both the OpenAI-compatible and the
Sonder Inference gateway (and :mod:`.sse_stream`'s ``post_streaming``) can use
one wall-clock-budgeted, non-redirecting exchange without an import cycle.

One exchange = one request and its bounded response.  The stdlib urllib stack
is kept (so HTTP errors keep the shape the shared gateway maps), with a
watchdog that shuts the socket down when the exchange's wall-clock budget is
spent.  ``_TRUST`` is thread-local: a scope applies to exchanges made on the
thread that entered it.
"""

from __future__ import annotations

import functools
import http.client
import socket
import ssl
import threading
import urllib.request
from contextlib import contextmanager
from typing import Iterator

from ..tls_contexts import default_https_context

DEFAULT_TIMEOUT_SECONDS = 300.0
DETAIL_LIMIT = 240
# Error bodies are only ever inspected for a machine-readable code, never
# stored or relayed; a successful body larger than RESPONSE_BODY_LIMIT is not a
# response this runtime will buffer.
ERROR_BODY_LIMIT = 16_384
RESPONSE_BODY_LIMIT = 16 * 1024 * 1024
_READ_CHUNK = 65_536


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


class _ExchangeBudget:
    """Shut down an exchange's sockets once its total budget is spent."""

    def __init__(self, seconds: float) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self.expired = False
        self._timer = threading.Timer(max(0.0, float(seconds)), self._expire)
        self._timer.daemon = True

    def __enter__(self) -> "_ExchangeBudget":
        self._timer.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._timer.cancel()

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.append(sock)
            expired = self.expired
        if expired:
            _shutdown(sock)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


class _BudgetedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, *args, budget: _ExchangeBudget, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._budget = budget

    def connect(self) -> None:
        super().connect()
        self._budget.attach(self.sock)


class _BudgetedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, *args, budget: _ExchangeBudget, **kwargs) -> None:  # type: ignore[no-untyped-def]
        super().__init__(*args, **kwargs)
        self._budget = budget

    def connect(self) -> None:
        super().connect()
        self._budget.attach(self.sock)


class _BudgetedHTTPHandler(urllib.request.HTTPHandler):
    def __init__(self, budget: _ExchangeBudget) -> None:
        super().__init__()
        self._budget = budget

    def http_open(self, req):  # type: ignore[no-untyped-def]
        return self.do_open(
            functools.partial(_BudgetedHTTPConnection, budget=self._budget), req,
        )


_TRUST = threading.local()


@contextmanager
def trust_scope(ca_bundle: str) -> Iterator[None]:
    """Verify HTTPS exchanges on this thread against exactly ``ca_bundle``.

    A private worker names its own CA bundle; inside this scope the bundle is
    the only trust anchor (the system store is not consulted), so a private
    worker's URL cannot be satisfied by a publicly issued certificate.  An
    empty value keeps the system trust store.  Scopes nest and always restore.
    """
    previous = getattr(_TRUST, "ca_bundle", "")
    _TRUST.ca_bundle = ca_bundle or ""
    try:
        yield
    finally:
        _TRUST.ca_bundle = previous


def _tls_context() -> ssl.SSLContext:
    ca_bundle = getattr(_TRUST, "ca_bundle", "")
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    return default_https_context()


class _BudgetedHTTPSHandler(urllib.request.HTTPSHandler):
    def __init__(self, budget: _ExchangeBudget) -> None:
        # Owned here: the stdlib keeps its copy in a private attribute.
        self._tls = _tls_context()
        super().__init__(context=self._tls)
        self._budget = budget

    def https_open(self, req):  # type: ignore[no-untyped-def]
        return self.do_open(
            functools.partial(_BudgetedHTTPSConnection, budget=self._budget), req,
            context=self._tls,
        )


def _read_bounded(stream, limit: int) -> bytes:  # type: ignore[no-untyped-def]
    """Read at most ``limit + 1`` bytes so an oversize body is detectable."""
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = stream.read(min(_READ_CHUNK, limit + 1 - total))
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks)


def _bounded(text: object) -> str:
    value = " ".join(str(text or "").split())
    return value if len(value) <= DETAIL_LIMIT else value[: DETAIL_LIMIT - 3] + "..."


def _error_document_fields(document: object, *, bound_message: bool = True) -> tuple[str, str]:
    error = document.get("error") if isinstance(document, dict) else None
    if not isinstance(error, dict):
        return "", ""
    code = error.get("code")
    message = error.get("message") or error.get("detail")
    message = message if isinstance(message, str) else ""
    return (
        code if isinstance(code, str) else "",
        _bounded(message) if bound_message else message,
    )
