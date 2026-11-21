"""Run a TLS listener's handshake on the connection thread, under a timeout.

``SSLContext.wrap_socket`` on a listening socket defaults to
``do_handshake_on_connect=True``, so ``accept()`` performs the whole server
handshake.  ``socketserver`` calls ``accept()`` on the single
``serve_forever`` thread, and nothing bounds the handshake, so one peer that
connects and never sends a ClientHello stalls every later connection before
any authentication runs.

``wrap_listener`` defers the handshake, and ``DeferredTLSHandshakeMixin``
completes it in ``finish_request``, which a threading server runs on the
per-connection worker thread, bounded by ``tls_handshake_timeout``.  A peer
that fails or stalls the handshake is dropped; the caller's
``shutdown_request`` closes its socket.
"""
from __future__ import annotations

import socket
import socketserver
import ssl
from typing import Any

TLS_HANDSHAKE_TIMEOUT_SECONDS = 10.0


def wrap_listener(context: ssl.SSLContext, sock: socket.socket) -> ssl.SSLSocket:
    """Wrap a listening socket so ``accept()`` never performs the handshake."""
    return context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False)


def complete_handshake(request: ssl.SSLSocket, timeout: float) -> bool:
    """Finish a deferred server handshake; ``False`` when the peer failed it."""
    previous = request.gettimeout()
    try:
        request.settimeout(timeout)
        request.do_handshake()
        request.settimeout(previous)
    except OSError:
        # ssl.SSLError and TimeoutError are both OSError subclasses.
        return False
    return True


class DeferredTLSHandshakeMixin(socketserver.BaseServer):
    """Mix in before ``ThreadingHTTPServer`` when its socket is ``wrap_listener``-ed."""

    tls_handshake_timeout: float = TLS_HANDSHAKE_TIMEOUT_SECONDS

    def finish_request(
        self,
        request: socket.socket | tuple[bytes, socket.socket],
        client_address: Any,
    ) -> None:
        if isinstance(request, ssl.SSLSocket) and not complete_handshake(
            request, self.tls_handshake_timeout
        ):
            return
        super().finish_request(request, client_address)


__all__ = [
    "DeferredTLSHandshakeMixin",
    "TLS_HANDSHAKE_TIMEOUT_SECONDS",
    "complete_handshake",
    "wrap_listener",
]
