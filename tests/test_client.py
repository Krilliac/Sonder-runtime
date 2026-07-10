import json
import urllib.error

import trilobite_client as tc


def test_build_request_with_key():
    url, headers, body = tc.build_request("http://h:1", "k", "hi")
    assert url == "http://h:1/v1/chat/completions"
    assert headers["Authorization"] == "Bearer k"
    assert headers["Content-Type"] == "application/json"
    obj = json.loads(body.decode("utf-8"))
    assert obj["model"] == "trilobite"
    assert obj["messages"] == [{"role": "user", "content": "hi"}]
    assert obj["stream"] is False


def test_build_request_no_key():
    url, headers, body = tc.build_request("http://h:1", "", "hi")
    assert "Authorization" not in headers
    obj = json.loads(body.decode("utf-8"))
    assert obj["messages"][0]["content"] == "hi"


def test_build_request_strips_trailing_slash():
    url, _, _ = tc.build_request("http://h:1/", "", "hi")
    assert url == "http://h:1/v1/chat/completions"


def test_send_prompt_falls_back_to_local_on_connection_error(monkeypatch):
    calls = []
    monkeypatch.setenv("TRILOBITE_FALLBACK_LOCAL", "1")

    def fake_send(server, key, prompt):
        calls.append((server, key, prompt))
        if server == "http://hosted":
            raise urllib.error.URLError("offline")
        return "local reply"

    monkeypatch.setattr(tc, "send_prompt", fake_send)
    reply, used, warning = tc.send_prompt_with_fallback(
        "http://hosted",
        "hosted-key",
        "hi",
        fallback_server="http://127.0.0.1:11435",
    )

    assert reply == "local reply"
    assert used == "http://127.0.0.1:11435"
    assert "Fell back to local server" in warning
    assert calls == [
        ("http://hosted", "hosted-key", "hi"),
        ("http://127.0.0.1:11435", "", "hi"),
    ]


def test_send_prompt_does_not_fallback_on_http_error(monkeypatch):
    def fake_send(server, key, prompt):
        raise urllib.error.HTTPError(server, 401, "no", {}, None)

    monkeypatch.setattr(tc, "send_prompt", fake_send)

    try:
        tc.send_prompt_with_fallback("http://hosted", "bad-key", "hi")
    except urllib.error.HTTPError as e:
        assert e.code == 401
    else:
        raise AssertionError("expected HTTPError")
