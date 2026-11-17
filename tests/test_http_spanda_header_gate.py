"""``X-Sonder-Spanda: 1`` is a developer-only request opt-in.

The header makes one chat turn draw ``k`` model samples instead of one.
Every other multi-call route on ``/v1/chat/completions`` (natural fanout and
ensembles) needs developer authority, so a plain served account must not be
able to multiply its model spend with a header. For such a caller the header
is ignored: one sample, no Spanda response headers. The operator's own
``[spanda] enabled`` configuration still applies to every caller.
"""
from __future__ import annotations

import json

import pytest

import sonder_runtime.interfaces.http.serve as ts
from tests.test_serve_auth import _http_server, _request

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("role, samples, honoured", [
    ("user", 1, False),
    ("developer", 3, True),
    ("admin", 3, True),
])
def test_spanda_header_is_honoured_only_for_developer_callers(monkeypatch, role, samples, honoured):
    monkeypatch.setattr(ts, "API_KEY", "")
    monkeypatch.setattr(ts, "AUTH_MODE", "account")
    monkeypatch.setattr(ts, "REQUIRE_ACCOUNT", True)
    monkeypatch.setattr(ts, "_auth_account",
                        lambda _header: {"username": "spanda-" + role, "role": role})
    monkeypatch.setattr(ts.Handler, "_auth_rate_limited", lambda self: False)

    class FakeConnection:
        def close(self):
            pass

    monkeypatch.setattr(ts._legacy_runtime(), "_open_db", lambda: FakeConnection())
    monkeypatch.setattr(ts.admin_auth, "rate_limit", lambda conn, account: (True, ""))
    monkeypatch.setattr(ts._legacy_runtime(), "chat_web_response", lambda *a, **k: None)
    calls = []
    monkeypatch.setattr(ts._legacy_runtime(), "answer_with_history",
                        lambda prompt, history, **_k: calls.append(prompt) or "FOUR")
    body = json.dumps({"model": "sonder",
                       "messages": [{"role": "user", "content": "what is two plus two"}]})
    with _http_server(monkeypatch) as port:
        status, headers, payload = _request(
            port, "POST", "/v1/chat/completions", body=body,
            headers={"Content-Type": "application/json", "Authorization": "Bearer t",
                     "X-Sonder-Spanda": "1"})
    assert status == 200, payload
    assert json.loads(payload)["choices"][0]["message"]["content"].startswith("FOUR")
    assert len(calls) == samples
    assert ("X-Sonder-Spanda-Rsc" in headers) is honoured
