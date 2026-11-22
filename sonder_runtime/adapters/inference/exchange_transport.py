"""Direct HTTP exchange primitives shared by the inference gateways.

A leaf module: it imports neither gateway, so both the OpenAI-compatible and the
Sonder Inference gateway (and :mod:`.sse_stream`'s ``post_streaming``) can use
one wall-clock-budgeted, non-redirecting exchange without an import cycle.

One exchange = one request and its bounded response.  The stdlib urllib stack
is kept (so HTTP errors keep the shape the shared gateway maps), with a
watchdog that shuts the socket down when the exchange's wall-clock budget is
spent.  ``_TRUST`` is thread-local: a scope applies to exchanges made on the
thread that entered it.

Cooperative cancellation: inside :func:`_cancellation_scope` (a context
variable, so it follows the calling context), every exchange started on that
context watches the scope's token on a helper thread and closes its sockets
the moment the token reports cancelled, so a blocked request or read unblocks
at once instead of running on to its budget.
"""

from __future__ import annotations

import functools
import http.client
import logging
import socket
import ssl
import threading
import time
import urllib.request
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
from typing import Iterator

from ..tls_contexts import default_https_context

logger = logging.getLogger(__name__)

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


# The cancellation token of the operation running on this context, if any.
_ACTIVE_CANCELLATION: ContextVar[object | None] = ContextVar(
    "sonder_inference_active_cancellation", default=None,
)
_UNSET: object = object()

# How often the exchange watcher re-reads a cancellation token.  An Event is a
# memory read; any other token (a ``cancelled`` property or a callable, e.g. a
# fleet worker's check, which reads the fleet store) is read at most twice a
# second per in-flight exchange.  A composite is split into its parts and each
# part is read at its own rate, so an Event inside it is still noticed at once.
_EVENT_POLL_SECONDS = 0.05
_CHECK_POLL_SECONDS = 0.5
# Backoff between reads after a token's read raised (e.g. "database is locked").
_WATCH_RETRY_SECONDS: tuple[float, ...] = (0.5, 1.0, 2.0, 5.0)


def _is_cancelled(token: object | None) -> bool:
    """Read any supported token: an Event, a ``cancelled`` property or method, or a callable."""
    if token is None:
        return False
    is_set = getattr(token, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    value = getattr(token, "cancelled", _UNSET)
    if value is not _UNSET:
        # A property (OperationContext tokens) or a method (concurrent.futures.Future).
        return bool(value()) if callable(value) else bool(value)
    if callable(token):
        return bool(token())
    return False


class _CompositeCancellation:
    """Cancelled when either part is: an operation token plus a per-request token.

    Satisfies the ``CancellationToken`` protocol (``cancelled`` and ``wait``).
    Immutable after construction; safe to read from any thread.
    """

    def __init__(self, primary: object | None, secondary: object | None) -> None:
        self._primary = primary
        self._secondary = secondary

    @property
    def parts(self) -> tuple[object | None, object | None]:
        return self._primary, self._secondary

    @property
    def cancelled(self) -> bool:
        return _is_cancelled(self._primary) or _is_cancelled(self._secondary)

    def wait(self, timeout: float | None = None) -> bool:
        if timeout is None or timeout <= 0:
            return self.cancelled
        deadline = time.monotonic() + float(timeout)
        while not self.cancelled:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(_EVENT_POLL_SECONDS, remaining))
        return self.cancelled


def _cancel_leaves(token: object | None) -> list[object]:
    if isinstance(token, _CompositeCancellation):
        return [leaf for part in token.parts for leaf in _cancel_leaves(part)]
    return [] if token is None else [token]


def _leaf_poll_seconds(leaf: object) -> float:
    return _EVENT_POLL_SECONDS if callable(getattr(leaf, "is_set", None)) else _CHECK_POLL_SECONDS


@contextmanager
def _cancellation_scope(token: object | None) -> Iterator[None]:
    """Make ``token`` the cancellation every exchange on this context watches."""
    if token is None:
        yield
        return
    scope_token = _ACTIVE_CANCELLATION.set(token)
    try:
        yield
    finally:
        _ACTIVE_CANCELLATION.reset(scope_token)


def _cancelled_quietly(token: object | None) -> bool:
    """``_is_cancelled`` for a pre-send check: a failing read is not a cancel."""
    try:
        return _is_cancelled(token)
    except Exception:  # noqa: BLE001 - the watcher reads again, backs off and reports it
        return False


class _ExchangeBudget:
    """Shut down an exchange's sockets once its total budget is spent or it is cancelled.

    ``cancellation`` defaults to the token of the enclosing
    :func:`_cancellation_scope`.  With a token, a daemon watcher thread (run
    in a copy of the caller's context, so a check bound to a context variable
    reads the caller's binding) polls it and calls :meth:`abort`.  One budget
    per exchange, owned by the thread that entered it; ``attach``, ``abort``
    and the timer may run on other threads and synchronise on ``_lock``.
    """

    def __init__(self, seconds: float, *, cancellation: object | None = _UNSET) -> None:
        self._lock = threading.Lock()
        self._sockets: list[socket.socket] = []
        self.expired = False
        self.aborted = False
        self._cancellation: object | None = (
            _ACTIVE_CANCELLATION.get() if cancellation is _UNSET else cancellation
        )
        self._stop = threading.Event()
        self._cancel_thread: threading.Thread | None = None
        self._timer = threading.Timer(max(0.0, float(seconds)), self._expire)
        self._timer.daemon = True

    def __enter__(self) -> "_ExchangeBudget":
        self._timer.start()
        if self._cancellation is not None:
            if _cancelled_quietly(self._cancellation):
                self.abort()
            else:
                self._cancel_thread = threading.Thread(
                    target=copy_context().run, args=(self._watch_cancel,),
                    name="sonder-inference-cancel", daemon=True,
                )
                self._cancel_thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._timer.cancel()
        self._stop.set()
        thread = self._cancel_thread
        if thread is not None and threading.current_thread() is not thread:
            thread.join(timeout=0.5)

    def _watch_cancel(self) -> None:
        leaves = [(leaf, _leaf_poll_seconds(leaf)) for leaf in _cancel_leaves(self._cancellation)]
        if not leaves:
            return
        tick = min(interval for _, interval in leaves)
        next_read = [0.0] * len(leaves)
        failures = [0] * len(leaves)
        logged = False
        while not self._stop.wait(tick):
            now = time.monotonic()
            for index, (leaf, interval) in enumerate(leaves):
                if now < next_read[index]:
                    continue
                try:
                    cancelled = _is_cancelled(leaf)
                except Exception as exc:  # noqa: BLE001 - a failing read must not end the watcher
                    # A locked fleet store: back off, read again, say so once.
                    delay = _WATCH_RETRY_SECONDS[min(failures[index], len(_WATCH_RETRY_SECONDS) - 1)]
                    failures[index] += 1
                    next_read[index] = now + delay
                    if not logged:
                        logged = True
                        logger.warning("sonder-inference cancel watcher read failed, retrying: %s",
                                       _bounded(exc))
                    continue
                failures[index] = 0
                next_read[index] = now + interval
                if cancelled:
                    self.abort()
                    return

    def attach(self, sock: socket.socket) -> None:
        with self._lock:
            self._sockets.append(sock)
            expired = self.expired
            aborted = self.aborted
        if aborted:
            _close_socket(sock)
        elif expired:
            _shutdown(sock)

    def _expire(self) -> None:
        with self._lock:
            self.expired = True
            sockets = list(self._sockets)
        for sock in sockets:
            _shutdown(sock)

    def abort(self) -> None:
        """Close the attached sockets now so a blocked request or read unblocks."""
        with self._lock:
            self.aborted = True
            sockets = list(self._sockets)
        for sock in sockets:
            _close_socket(sock)


def _shutdown(sock: socket.socket) -> None:
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass


def _close_socket(sock: socket.socket) -> None:
    """Shut down, then close the descriptor itself.

    ``shutdown()`` unblocks a pending read on POSIX; Windows only aborts a
    blocking ``recv()`` on another thread when the descriptor is closed, and a
    response's file object keeps the socket open past ``socket.close()``, so
    the descriptor is detached and closed (the server then sees the
    disconnect).  The detached socket object reports EBADF afterwards rather
    than touching a reused descriptor number.
    """
    _shutdown(sock)
    try:
        descriptor = sock.detach()
        if descriptor >= 0:
            socket.close(descriptor)
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
