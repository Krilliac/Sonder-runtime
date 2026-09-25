"""The served listener must queue a burst long enough for admission to answer.

``socketserver`` defaults ``request_queue_size`` to 5.  With that backlog a
burst of ~60 concurrent clients overflowed the kernel accept queue and some
POSTs were reset at the TCP layer before the admission layer (4 slots plus a
32-deep queue by default) could accept them or answer 429.  Measured live on
the served runtime: 7 of 540 concurrent chat POSTs were reset.
"""
from __future__ import annotations

import collections
import concurrent.futures
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler

import sonder_runtime.interfaces.http.serve as serve


def test_served_listener_backlog_covers_default_admission_capacity():
    # Default admission capacity is SONDER_MAX_CONCURRENT_REQUESTS (4) plus
    # SONDER_QUEUE_DEPTH (32); the TCP backlog must not refuse sooner.
    assert serve.ServeHTTPServer.request_queue_size >= 4 + 32
    assert issubclass(serve.ServeHTTPServer, serve.ThreadingHTTPServer)
    assert serve.ServeHTTPServer.daemon_threads is True


def test_served_listener_accepts_a_concurrent_post_burst_without_resets():
    class _Echo(BaseHTTPRequestHandler):
        def do_POST(self):
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    httpd = serve.ServeHTTPServer(("127.0.0.1", 0), _Echo)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = "http://127.0.0.1:%d/" % httpd.server_address[1]

    def call(_):
        request = urllib.request.Request(
            url, data=b'{"a":1}', headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                response.read()
                return response.status
        except OSError as error:
            return type(error).__name__

    try:
        outcomes = collections.Counter()
        for _ in range(3):
            with concurrent.futures.ThreadPoolExecutor(60) as pool:
                outcomes.update(pool.map(call, range(180)))
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)
    assert outcomes == {200: 540}, outcomes
