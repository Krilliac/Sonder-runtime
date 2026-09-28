"""Bound the threads a threading HTTP listener spends on open connections.

``socketserver.ThreadingMixIn`` starts one thread per accepted connection
before any request byte is parsed.  Every real defence (origin check, auth
rate limit, body cap, admission slot) sits downstream of the request headers,
and the per-connection read timeout only bounds how long each idle connection
lives, not how many coexist.  A client that opens many connections and never
finishes a request could therefore grow threads and memory without limit.

``BoundedConnectionsMixin`` holds a slot for each connection from accept until
its handler thread finishes.  Past ``max_connections`` a connection gets a
minimal ``503`` with ``Retry-After`` and is closed on the accept thread, so no
handler thread is started for it.
"""
from __future__ import annotations

import threading

_REFUSAL = (
    b"HTTP/1.1 503 Service Unavailable\r\n"
    b"Content-Type: text/plain\r\n"
    b"Content-Length: 25\r\n"
    b"Retry-After: 1\r\n"
    b"Connection: close\r\n\r\n"
    b"too many open connections"
)


class BoundedConnectionsMixin:
    """Mix in before ``ThreadingHTTPServer`` to cap concurrent connections."""

    max_connections = 256

    def _connection_slots(self):
        slots = self.__dict__.get("_bounded_connection_slots")
        if slots is None:
            slots = self.__dict__.setdefault(
                "_bounded_connection_slots",
                threading.BoundedSemaphore(max(1, int(self.max_connections))),
            )
        return slots

    @property
    def active_connections(self):
        """Connections currently holding a slot (for diagnostics and tests)."""
        with self._active_lock():
            return self.__dict__.get("_bounded_active", 0)

    def _active_lock(self):
        lock = self.__dict__.get("_bounded_active_lock")
        if lock is None:
            lock = self.__dict__.setdefault("_bounded_active_lock", threading.Lock())
        return lock

    def _count(self, delta):
        with self._active_lock():
            self.__dict__["_bounded_active"] = self.__dict__.get("_bounded_active", 0) + delta

    def _release_slot(self):
        self._count(-1)
        self._connection_slots().release()

    def process_request(self, request, client_address):
        if not self._connection_slots().acquire(blocking=False):
            try:
                # A fresh socket's send buffer is empty; the timeout only
                # guarantees a hostile peer cannot stall the accept thread.
                request.settimeout(0.5)
                request.sendall(_REFUSAL)
            except OSError:
                pass
            self.shutdown_request(request)
            return
        self._count(1)
        try:
            super().process_request(request, client_address)
        except BaseException:
            # The handler thread never started, so it cannot release the slot.
            self._release_slot()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._release_slot()


__all__ = ["BoundedConnectionsMixin"]
