"""Private foreground HTTP socket ownership over actual managed workers."""
from http.server import ThreadingHTTPServer
import socket
import math
from threading import RLock

from ...platform.runtime_threads import OwnedRuntimeThreads


class ManagedHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    block_on_close = False

    def __init__(self, address, handler, *, workers, max_requests=32, request_timeout_seconds=5):
        if type(workers) is not OwnedRuntimeThreads:
            raise TypeError("exact host worker owner required")
        if type(max_requests) is not int or not 1 <= max_requests <= 32:
            raise ValueError("bounded HTTP request capacity required")
        if type(request_timeout_seconds) not in (int, float) or not math.isfinite(request_timeout_seconds) or not 0.1 <= request_timeout_seconds <= 5:
            raise ValueError("bounded managed HTTP read timeout required")
        self._workers = workers
        self._maximum = max_requests
        self._owned_requests = set()
        self._request_lock = RLock()
        self._requests_stopped = False
        self._socket_failure = False
        # socket shutdown alone does not cancel an already-blocked Windows
        # makefile read. Set the explicit owned profile's timeout at setup.
        owned_handler = type("ManagedRequestHandler", (handler,), {"timeout": request_timeout_seconds})
        super().__init__(address, owned_handler)

    def process_request(self, request, address):
        with self._request_lock:
            if self._requests_stopped or len(self._owned_requests) >= self._maximum:
                self.shutdown_request(request)
                return
            self._owned_requests.add(request)
        try:
            worker = self._workers.thread(
                target=self.process_request_thread, args=(request, address),
                name="sonder-owned-http-request", daemon=True,
            )
            worker.start()
        except BaseException:
            self.shutdown_request(request)
            raise

    def shutdown_request(self, request):
        try:
            try:
                request.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # Still close the exact disconnected/already-closed socket.
            self.close_request(request)
            closed = request.fileno() == -1
        except BaseException:
            with self._request_lock:
                self._socket_failure = True
            raise
        with self._request_lock:
            if closed:
                self._owned_requests.discard(request)
            # socket.makefile() may still own references while its handler
            # unwinds. Keep the exact socket pending until those close.

    def server_close(self):
        with self._request_lock:
            self._requests_stopped = True
            requests = tuple(self._owned_requests)
        super().server_close()
        # Close exact accepted sockets. Blocked readers may need their configured
        # timeout; handler termination remains a separate worker-owner proof.
        for request in requests:
            self.shutdown_request(request)

    @property
    def sockets_closed(self):
        with self._request_lock:
            self._owned_requests = {request for request in self._owned_requests if request.fileno() != -1}
            return self._requests_stopped and self.socket.fileno() == -1 and not self._owned_requests and not self._socket_failure
