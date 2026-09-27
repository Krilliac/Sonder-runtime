"""The thin client never exposes the bearer key to plaintext or redirects."""

import http.server
import json
import threading
import urllib.error
from contextlib import contextmanager

import pytest

import sonder_client
from sonder_runtime.adapters import client_request, client_transport

# Assembled at runtime so no secret-shaped literal lands in the repository.
_KEY = "-".join(("test", "client", "bearer", "value"))


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for name in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("no_proxy", "*")
    monkeypatch.setenv("NO_PROXY", "*")


@pytest.mark.parametrize(
    "url",
    [
        "https://sonder.example.com/v1/chat/completions",
        "https://10.0.0.5:8443",
        "http://127.0.0.1:11435/v1/chat/completions",
        "http://127.8.9.10:1",
        "http://localhost:11435",
        "http://LOCALHOST:11435",
        "http://[::1]:11435",
    ],
)
def test_key_transport_allows_https_and_loopback_http(url):
    client_request.require_secure_key_transport(url)


@pytest.mark.parametrize(
    "url",
    [
        "http://sonder.example.com/v1/chat/completions",
        "http://10.0.0.5:11435",
        "http://192.168.1.20:11435",
        "http://127.0.0.1.attacker.example",
        "http://localhost.attacker.example",
        "ftp://127.0.0.1/",
        "file:///etc/passwd",
        "sonder.example.com",
        "https://",
        "",
    ],
)
def test_key_transport_refuses_plaintext_remote_and_other_schemes(url):
    with pytest.raises(client_request.InsecureKeyTransportError):
        client_request.require_secure_key_transport(url)


def test_build_request_refuses_key_over_plaintext_remote():
    with pytest.raises(client_request.InsecureKeyTransportError):
        client_request.build_chat_request("http://sonder.example.com", _KEY, "hi")
    with pytest.raises(client_request.InsecureKeyTransportError):
        sonder_client.build_request("http://sonder.example.com", _KEY, "hi")


def test_build_request_without_key_keeps_plaintext_remote():
    url, headers, _body = client_request.build_chat_request(
        "http://sonder.example.com", "", "hi"
    )
    assert url == "http://sonder.example.com/v1/chat/completions"
    assert "Authorization" not in headers


def test_build_request_with_key_over_https_and_loopback():
    for server in ("https://sonder.example.com", "http://127.0.0.1:11435"):
        _url, headers, _body = client_request.build_chat_request(server, _KEY, "hi")
        assert headers["Authorization"] == "Bearer " + _KEY


def test_transport_rechecks_authorization_from_custom_builder(monkeypatch):
    opened = []
    monkeypatch.setattr(client_transport, "_open", opened.append)

    def builder(server, api_key, prompt):
        return (
            "http://sonder.example.com/v1/chat/completions",
            {"Authorization": "Bearer " + api_key},
            b"{}",
        )

    with pytest.raises(client_request.InsecureKeyTransportError):
        client_transport.send_chat_prompt(
            "ignored", _KEY, "hi", request_builder=builder
        )
    assert opened == []


def test_main_refuses_key_over_plaintext_remote_before_prompting(monkeypatch, capsys):
    monkeypatch.setattr(
        "builtins.input",
        lambda _prompt: pytest.fail("client prompted despite an insecure key URL"),
    )

    code = sonder_client.main(["--server", "http://sonder.example.com", "--key", _KEY])

    assert code == 2
    out = capsys.readouterr().out
    assert "refusing to send the API key" in out
    assert _KEY not in out


@contextmanager
def _serve(handler_cls):
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=10)


def _recording_target(seen):
    class Target(http.server.BaseHTTPRequestHandler):
        def _reply(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen.append((self.command, self.headers.get("Authorization")))
            payload = json.dumps(
                {"choices": [{"message": {"content": "from-target"}}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        do_GET = _reply
        do_POST = _reply

        def log_message(self, *_args):
            return None

    return Target


def _redirector(code, location):
    class Redirector(http.server.BaseHTTPRequestHandler):
        def _redirect(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            self.send_response(code)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        do_GET = _redirect
        do_POST = _redirect

        def log_message(self, *_args):
            return None

    return Redirector


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_authenticated_request_never_follows_cross_origin_redirect(code):
    seen = []
    with _serve(_recording_target(seen)) as target:
        with _serve(_redirector(code, target + "/v1/chat/completions")) as origin:
            with pytest.raises(urllib.error.HTTPError) as excinfo:
                sonder_client.send_prompt(origin, _KEY, "hi")

    assert excinfo.value.code == code
    body = excinfo.value.read().decode("utf-8")
    assert "refusing to follow HTTP %d redirect" % code in body
    assert _KEY not in body
    assert seen == []


def test_authenticated_redirect_refusal_does_not_fall_back_to_local():
    seen = []
    with _serve(_recording_target(seen)) as target:
        with _serve(_redirector(302, target + "/v1/chat/completions")) as origin:
            with pytest.raises(urllib.error.HTTPError):
                sonder_client.send_prompt_with_fallback(
                    origin, _KEY, "hi", fallback_server=target
                )
    assert seen == []


def test_unauthenticated_request_still_follows_redirects():
    seen = []
    with _serve(_recording_target(seen)) as target:
        with _serve(_redirector(302, target + "/v1/chat/completions")) as origin:
            reply = sonder_client.send_prompt(origin, "", "hi")

    assert reply == "from-target"
    # urllib turns a redirected POST into a GET; no credential travels with it.
    assert seen == [("GET", None)]


def test_authenticated_request_without_redirect_reaches_loopback_server():
    seen = []
    with _serve(_recording_target(seen)) as target:
        reply = sonder_client.send_prompt(target, _KEY, "hi")

    assert reply == "from-target"
    assert seen == [("POST", "Bearer " + _KEY)]


def _recording_proxy(seen):
    class Proxy(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length:
                self.rfile.read(length)
            seen.append((self.path, self.headers.get("Authorization")))
            payload = json.dumps(
                {"choices": [{"message": {"content": "from-proxy"}}]}
            ).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_args):
            return None

    return Proxy


def test_keyed_loopback_request_bypasses_environment_http_proxy(monkeypatch):
    # Undo the autouse no_proxy='*': urllib has no implicit loopback bypass.
    for name in ("no_proxy", "NO_PROXY"):
        monkeypatch.delenv(name, raising=False)
    proxied, direct = [], []
    with _serve(_recording_proxy(proxied)) as proxy:
        monkeypatch.setenv("http_proxy", proxy)
        monkeypatch.setenv("HTTP_PROXY", proxy)
        with _serve(_recording_target(direct)) as target:
            # Control: an unauthenticated request does use the proxy, so the
            # proxy wiring in this test is live.
            assert sonder_client.send_prompt(target, "", "hi") == "from-proxy"
            assert proxied == [(target + "/v1/chat/completions", None)]

            reply = sonder_client.send_prompt(target, _KEY, "hi")

    assert reply == "from-target"
    assert direct == [("POST", "Bearer " + _KEY)]
    assert all(auth is None for _path, auth in proxied)
    assert len(proxied) == 1
