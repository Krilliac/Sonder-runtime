"""``/v1/sonder/ollama-pool`` uses the shared failed-auth admission.

Every other admin-only route (``/v1/compute/nodes/refresh`` is the model)
checks the per-client failed-authentication bucket before it authenticates,
and charges an unauthenticated attempt to it. The pool route did neither, so
it answered an unlimited stream of credential guesses with a distinguishable
200 / 403 and never tripped the limiter.
"""
from __future__ import annotations

import json

import pytest

import sonder_runtime.interfaces.http.serve as ts
from tests.test_serve_auth import _http_server, _request

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("method", ["GET", "POST"])
def test_pool_admin_uses_shared_failed_auth_admission(monkeypatch, method):
    failures = []
    auth_calls = []
    lifecycle = ts.sonder_lifecycle.get()
    monkeypatch.setattr(lifecycle, "auth_attempt_allowed", lambda client: not failures)
    monkeypatch.setattr(lifecycle, "record_auth_failure",
                        lambda client, reason: failures.append(reason))

    def denied(self):
        auth_calls.append(True)
        return {"authorized": False, "mode": "api-key", "api_key": False, "account": None}

    monkeypatch.setattr(ts.Handler, "_request_auth_context", denied)
    monkeypatch.setattr(ts, "_ollama_pool_admin_page",
                        lambda *a, **k: pytest.fail("denied request reached the pool"))
    body = "{}" if method == "POST" else None
    headers = {"Content-Type": "application/json"} if method == "POST" else {}
    with _http_server(monkeypatch) as port:
        first, _, first_body = _request(port, method, "/v1/sonder/ollama-pool",
                                        body=body, headers=headers)
        second, _, _ = _request(port, method, "/v1/sonder/ollama-pool",
                                body=body, headers=headers)
    assert first == 401, first_body
    assert json.loads(first_body)["error"]["code"] == "UNAUTHENTICATED"
    assert second == 429
    assert auth_calls == [True]
    assert len(failures) == 1


def test_authenticated_non_admin_is_forbidden_and_not_charged(monkeypatch):
    failures = []
    lifecycle = ts.sonder_lifecycle.get()
    monkeypatch.setattr(lifecycle, "auth_attempt_allowed", lambda client: True)
    monkeypatch.setattr(lifecycle, "record_auth_failure",
                        lambda client, reason: failures.append(reason))
    monkeypatch.setattr(ts.Handler, "_request_auth_context", lambda self: {
        "authorized": True, "mode": "account", "api_key": False,
        "account": {"username": "reader", "role": "user"}})
    with _http_server(monkeypatch) as port:
        status, headers, body = _request(port, "GET", "/v1/sonder/ollama-pool")
    assert status == 403
    assert json.loads(body) == {"error": "authorization"}
    assert headers["Cache-Control"] == "no-store"
    assert failures == []
