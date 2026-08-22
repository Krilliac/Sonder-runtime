import json

import sonder_client
from sonder_runtime.adapters import client_transport


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return json.dumps(self._payload).encode("utf-8")


def test_client_transport_adapter_owns_send_prompt(monkeypatch):
    seen = []

    def fake_builder(server, api_key, prompt):
        seen.append((server, api_key, prompt))
        return "http://host/v1/chat/completions", {"X-Test": "yes"}, b"{}"

    def fake_urlopen(request):
        assert request.full_url == "http://host/v1/chat/completions"
        assert request.get_header("X-test") == "yes"
        assert request.data == b"{}"
        return _Response({"choices": [{"message": {"content": "reply"}}]})

    monkeypatch.setattr(client_transport.urllib.request, "urlopen", fake_urlopen)

    assert sonder_client._send_chat_prompt is client_transport.send_chat_prompt
    assert client_transport.send_chat_prompt(
        "server", "key", "hello", request_builder=fake_builder
    ) == "reply"
    assert seen == [("server", "key", "hello")]


def test_root_send_prompt_preserves_request_builder_compatibility(monkeypatch):
    seen = []

    def fake_builder(server, api_key, prompt):
        seen.append((server, api_key, prompt))
        return "http://host/chat", {}, b"payload"

    monkeypatch.setattr(sonder_client, "build_request", fake_builder)
    monkeypatch.setattr(
        client_transport.urllib.request,
        "urlopen",
        lambda request: _Response(
            {"choices": [{"message": {"content": "delegated"}}]}
        ),
    )

    assert sonder_client.send_prompt("server", "key", "hello") == "delegated"
    assert seen == [("server", "key", "hello")]
