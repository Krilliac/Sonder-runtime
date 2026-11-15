"""/app/ over a real listener: headers, auth boundary, host and origin gates."""

from __future__ import annotations

import http.client
import json

import pytest

import sonder_runtime.interfaces.http.serve as ts
from sonder_runtime.interfaces.http import web_app
from tests.test_serve_host_policy import API_KEY, _server
from tests.web_app_fixtures import write_release


@pytest.fixture()
def bundle(tmp_path):
    web_app.clear_cache()
    write_release(tmp_path)
    yield tmp_path
    web_app.clear_cache()


def _enable(monkeypatch, root, *, enabled=True):
    monkeypatch.setattr(ts, "_WEB_APP", web_app.WebAppSettings(enabled, root))


def _request(port, method, path, *, host=None, headers=None, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.putrequest(method, path, skip_host=True)
        conn.putheader("Host", host if host is not None else "127.0.0.1:%d" % port)
        for name, value in (headers or {}).items():
            conn.putheader(name, value)
        if body is not None:
            conn.putheader("Content-Type", "application/json")
            conn.putheader("Content-Length", str(len(body)))
        conn.endheaders(body)
        response = conn.getresponse()
        return response.status, dict(response.getheaders()), response.read()
    finally:
        conn.close()


def test_disabled_is_an_ordinary_404(monkeypatch, bundle):
    _enable(monkeypatch, bundle, enabled=False)
    with _server(monkeypatch) as port:
        status, _headers, body = _request(port, "GET", "/app/")
        other_status, _h, other_body = _request(port, "GET", "/not-a-route/")
        assert status == other_status == 404
        assert "WEB_APP" not in body.decode()
        assert json.loads(body).keys() == json.loads(other_body).keys()


def test_enabled_without_bundle_names_the_remedy(monkeypatch, tmp_path):
    web_app.clear_cache()
    _enable(monkeypatch, tmp_path)
    with _server(monkeypatch) as port:
        status, _headers, body = _request(port, "GET", "/app/")
    assert status == 404
    error = json.loads(body)["error"]
    assert error["code"] == "WEB_APP_NOT_BUILT" and "build_web_app.py" in error["remedy"]


def test_enabled_serves_shell_with_security_headers(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        status, headers, body = _request(port, "GET", "/app/")
        assert status == 200 and body.startswith(b"<!doctype html>")
        lowered = {k.lower(): v for k, v in headers.items()}
        for name, value in web_app.SECURITY_HEADERS:
            assert lowered[name.lower()] == value, name
        assert lowered["cache-control"] == "no-cache"
        assert lowered["content-type"] == "text/html; charset=utf-8"
        assert "access-control-allow-origin" not in lowered
        etag = lowered["etag"]
        status, headers, body = _request(port, "GET", "/app/", headers={"If-None-Match": etag})
        assert status == 304 and body == b""
        status, headers, _ = _request(port, "GET", "/app")
        assert status == 308 and headers["Location"] == "/app/"
        status, headers, body = _request(port, "GET", "/app/canvaskit/canvaskit.wasm")
        assert status == 200 and headers["Content-Type"] == "application/wasm"
        assert body == b"\x00asm\x01\x00\x00\x00"
        status, _h, _b = _request(port, "GET", "/app/%2e%2e/server.py")
        assert status == 404


def test_rebinding_host_gets_421_on_app(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch) as port:
        status, _h, body = _request(port, "GET", "/app/", host="rebind.attacker.example:%d" % port)
    assert status == 421 and json.loads(body)["error"]["code"] == "HOST_NOT_ALLOWED"


def test_shell_needs_no_credentials_but_api_stays_gated(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch, api_key=API_KEY) as port:
        assert _request(port, "GET", "/app/")[0] == 200
        assert _request(port, "GET", "/v1/sonder/status")[0] == 401


def _post(port, origin, *, host=None):
    return _request(port, "POST", "/v1/chat/completions", host=host,
                    headers={"Origin": origin} if origin else {}, body=b"{}")


def test_same_origin_post_passes_origin_gate_only_when_enabled(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    with _server(monkeypatch, api_key=API_KEY) as port:
        status, headers, body = _post(port, "http://127.0.0.1:%d" % port)
        assert status == 401, body
        assert "Access-Control-Allow-Origin" not in headers
        assert _post(port, "http://evil.example")[0] == 403
        assert _post(port, "null")[0] == 403
        assert _post(port, "https://127.0.0.1:%d" % port)[0] == 403
    _enable(monkeypatch, bundle, enabled=False)
    with _server(monkeypatch, api_key=API_KEY) as port:
        status, _h, body = _post(port, "http://127.0.0.1:%d" % port)
        assert status == 403 and json.loads(body)["error"]["code"] == "forbidden_origin"


def test_rebinding_name_with_matching_origin_is_still_refused(monkeypatch, bundle):
    _enable(monkeypatch, bundle)
    name = "rebind.attacker.example:%d"
    with _server(monkeypatch, api_key=API_KEY) as port:
        host = name % port
        status, _h, body = _post(port, "http://" + host, host=host)
        assert status == 403 and json.loads(body)["error"]["code"] == "forbidden_origin"
