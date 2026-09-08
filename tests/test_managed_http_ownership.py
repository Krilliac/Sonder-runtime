from http.server import BaseHTTPRequestHandler
import socket
from threading import Event
from urllib.request import urlopen

from sonder_runtime.bootstrap.managed_http import ManagedHTTPServer
from sonder_runtime.platform.runtime_threads import OwnedRuntimeThreads


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def test_real_owned_http_request_workers_and_sockets_close():
    workers = OwnedRuntimeThreads(cleanup=lambda: True)
    server = ManagedHTTPServer(("127.0.0.1", 0), Handler, workers=workers)
    serving = workers.thread(target=lambda: server.serve_forever(poll_interval=0.01))
    serving.start()
    try:
        with urlopen("http://127.0.0.1:%s/" % server.server_port, timeout=2) as response:
            assert response.read() == b"ok"
    finally:
        server.shutdown()
        server.server_close()
    assert server.sockets_closed
    assert workers.close(timeout=2).clean
    assert not serving.is_alive()


def test_partial_request_socket_remains_owned_until_configured_read_timeout():
    accepted = Event()
    class PartialHandler(Handler):
        def handle(self):
            accepted.set()
            super().handle()
    workers = OwnedRuntimeThreads(cleanup=lambda: True)
    server = ManagedHTTPServer(("127.0.0.1", 0), PartialHandler, workers=workers, request_timeout_seconds=0.1)
    serving = workers.thread(target=lambda: server.serve_forever(poll_interval=0.01))
    serving.start()
    peer = socket.create_connection(server.server_address, timeout=2)
    try:
        assert accepted.wait(2)
        server.shutdown()
        server.server_close()
        assert workers.close(timeout=2).clean
        assert server.sockets_closed
    finally:
        peer.close()
        server.server_close()


def test_handler_file_references_remain_pending_until_real_worker_exit():
    accepted, release = Event(), Event()
    class HeldHandler(Handler):
        def handle(self):
            accepted.set()
            release.wait(3)
    workers = OwnedRuntimeThreads(cleanup=lambda: True)
    server = ManagedHTTPServer(("127.0.0.1", 0), HeldHandler, workers=workers)
    serving = workers.thread(target=lambda: server.serve_forever(poll_interval=0.01))
    serving.start()
    peer = socket.create_connection(server.server_address, timeout=2)
    try:
        assert accepted.wait(2)
        server.shutdown()
        server.server_close()
        assert not server.sockets_closed
        assert not workers.close(timeout=0.01).clean
    finally:
        release.set()
        peer.close()
    assert workers.close(timeout=2).clean
    assert server.sockets_closed
