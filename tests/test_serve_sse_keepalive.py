"""A streamed chat turn commits to SSE and keeps the connection alive while it generates."""

from contextlib import contextmanager
import json
import socket
import threading
import time

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.interfaces.http.sse import SSEKeepAlive
from sonder_runtime.platform.runtime_threads import Thread as owned_runtime_thread


@contextmanager
def _http_server(monkeypatch):
    monkeypatch.setattr(ts, "_maybe_live_reload", lambda: None)
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(ts, "STREAM_HEARTBEAT_SECONDS", 0.2)
    monkeypatch.setattr(ts.server, "chat_web_response", lambda *args, **kwargs: None)
    httpd = ts.ThreadingHTTPServer(("127.0.0.1", 0), ts.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _stream_request(port, *, on_received=None):
    """POST a streamed chat turn; return the raw response once the server closes.

    ``on_received`` sees everything received so far after each read.  The
    socket timeout only guards against a hang.  The first byte is not timed:
    it follows all the work the server does before committing to the stream
    (Host-name and model-catalog lookups, store setup), which takes well under
    a second on Linux but seconds on Windows, where a refused loopback
    connection (the harness's unreachable Ollama) alone takes about 2 s.
    """
    body = json.dumps({
        "model": "sonder", "stream": True,
        "messages": [{"role": "user", "content": "hello"}],
    }).encode("utf-8")
    request = (
        b"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
        b"Content-Type: application/json\r\nContent-Length: %d\r\n\r\n"
        % (port, len(body))
    ) + body
    received = b""
    with socket.create_connection(("127.0.0.1", port), timeout=30) as sock:
        sock.sendall(request)
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                break
            received += chunk
            if on_received is not None:
                on_received(received)
    return received.decode("utf-8")


def test_stream_headers_and_keepalives_arrive_before_generation_finishes(monkeypatch):
    # The answer is held back until the client has the response head and two
    # keep-alive frames, so a server that sends nothing until the answer is
    # ready fails here, on a fast host or a slow one.
    client_has_stream = threading.Event()
    seen_while_generating = []

    def answer_once_client_has_stream(*_args, **_kwargs):
        seen_while_generating.append(client_has_stream.wait(10))
        return "late answer"

    def on_received(data):
        _head, separator, stream = data.partition(b"\r\n\r\n")
        if separator and stream.count(b": keep-alive") >= 2:
            client_has_stream.set()

    monkeypatch.setattr(ts.server, "answer_with_history", answer_once_client_has_stream)
    with _http_server(monkeypatch) as port:
        text = _stream_request(port, on_received=on_received)

    # The head and two keep-alives reached the client while generation was
    # still running, not after it finished.
    assert seen_while_generating[:1] == [True]
    head, _, stream = text.partition("\r\n\r\n")
    assert head.startswith("HTTP/1.0 200") or head.startswith("HTTP/1.1 200")
    assert "text/event-stream" in head
    assert stream.count(": keep-alive") >= 2
    events = [line[6:] for line in stream.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    chunks = [json.loads(e) for e in events[:-1]]
    assert chunks[0]["choices"][0]["delta"]["content"] == "late answer"
    assert chunks[1]["choices"][0]["finish_reason"] == "stop"


def test_model_error_after_commit_is_a_terminal_sse_error(monkeypatch):
    def failing(*_args, **_kwargs):
        time.sleep(0.5)
        raise ts.ModelCallError("timeout", "model timed out", attempts=1)

    monkeypatch.setattr(ts.server, "answer_with_history", failing)
    with _http_server(monkeypatch) as port:
        text = _stream_request(port)

    head, _, stream = text.partition("\r\n\r\n")
    assert " 200 " in head.splitlines()[0]
    events = [line[6:] for line in stream.splitlines() if line.startswith("data: ")]
    assert events[-1] == "[DONE]"
    error = json.loads(events[-2])
    assert error["object"] == "error"
    assert error["error"]["code"] == "MODEL_CALL_504"
    assert error["error"]["message"] == "model timed out"


def test_non_stream_path_is_unchanged(monkeypatch):
    monkeypatch.setattr(ts.server, "answer_with_history", lambda *a, **k: "plain")
    body = json.dumps({"model": "sonder", "messages": [{"role": "user", "content": "hi"}]})
    import http.client
    with _http_server(monkeypatch) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        conn.request("POST", "/v1/chat/completions", body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read())
        conn.close()
    assert response.status == 200
    assert payload["choices"][0]["message"]["content"] == "plain"


def test_keepalive_detects_a_departed_client():
    frames = []

    def write(frame):
        if frames:
            raise BrokenPipeError()
        frames.append(frame)

    keepalive = SSEKeepAlive(write, 0.05, thread_factory=owned_runtime_thread).start()
    time.sleep(0.4)
    assert keepalive.stop() is False
    assert frames == [b": keep-alive\n\n"]
