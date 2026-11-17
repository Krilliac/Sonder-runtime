"""Deeply nested request JSON is malformed input, not a server failure.

``json.loads`` raises ``RecursionError`` (a ``RuntimeError``) on a document
nested past the interpreter's recursion limit. ``_read_json`` mapped only
decode errors to its 400, so such a body escaped the request handler. It is
the same client error as any other undecodable body.
"""
from __future__ import annotations

import http.client
import json

import pytest

import sonder_runtime.interfaces.http.serve as ts
from tests.test_serve_auth import _http_server

pytestmark = pytest.mark.unit

DEPTH = 100_000


@pytest.mark.parametrize("body", [
    b"[" * DEPTH,
    b"[" * DEPTH + b"]" * DEPTH,
    b'{"a":' * DEPTH + b"1" + b"}" * DEPTH,
], ids=["unclosed-arrays", "closed-arrays", "objects"])
def test_deeply_nested_json_is_the_malformed_json_400(monkeypatch, body):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "local-open")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", False)
    monkeypatch.setattr(
        ts._legacy_runtime(), "answer_with_history",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("must not route malformed JSON")),
    )
    assert len(body) <= ts.MAX_REQUEST_BYTES
    with _http_server(monkeypatch) as port:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        try:
            conn.request("POST", "/v1/chat/completions", body=body,
                         headers={"Content-Type": "application/json"})
            response = conn.getresponse()
            status, payload = response.status, response.read()
        finally:
            conn.close()
    assert status == 400, payload
    assert json.loads(payload)["error"] == {
        "message": "request body must contain valid JSON",
        "type": "invalid_request",
    }
