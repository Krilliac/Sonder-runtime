"""The served listener bounds concurrent connection threads.

``ThreadingHTTPServer`` gives every accepted connection its own thread before
a byte is parsed, so a client holding many connections open without finishing
a request (each bounded only by the per-connection read timeout) could grow
threads and memory without limit.  Past the cap a connection is answered 503
and closed immediately; a slot frees when its connection finishes.
"""
import http.client
import socket
import threading
import time

from sonder_runtime.interfaces.http import serve as ts


class _Ok(ts.BaseHTTPRequestHandler):
    timeout = 10

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def _server(limit):
    class Limited(ts.ServeHTTPServer):
        max_connections = limit

    httpd = Limited(("127.0.0.1", 0), _Ok)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def test_connections_past_the_cap_get_503_and_slots_are_released():
    httpd = _server(2)
    port = httpd.server_address[1]
    idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
    try:
        assert _wait(lambda: httpd.active_connections == 2)
        refused = socket.create_connection(("127.0.0.1", port))
        refused.settimeout(5)
        reply = refused.recv(256)
        refused.close()
        assert reply.startswith(b"HTTP/1.1 503")
        assert httpd.active_connections == 2
    finally:
        for sock in idle:
            sock.close()
    assert _wait(lambda: httpd.active_connections == 0)
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.request("GET", "/")
    assert conn.getresponse().status == 200
    conn.close()
    httpd.shutdown()
    httpd.server_close()


def test_default_cap_is_bounded():
    assert 16 <= ts.ServeHTTPServer.max_connections <= 4096
