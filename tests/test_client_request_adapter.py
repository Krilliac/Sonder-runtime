import json

import sonder_client
from sonder_runtime.adapters import client_request


def test_client_request_adapter_owns_builder():
    assert sonder_client._build_chat_request is client_request.build_chat_request
    assert sonder_client.build_request("http://host/", "key", "hello") == (
        *client_request.build_chat_request("http://host/", "key", "hello"),
    )


def test_client_request_adapter_builds_unauthenticated_chat_payload():
    url, headers, body = client_request.build_chat_request(
        "http://host", "", "hello"
    )

    assert url == "http://host/v1/chat/completions"
    assert headers == {"Content-Type": "application/json"}
    assert json.loads(body.decode("utf-8")) == {
        "model": "sonder",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": False,
    }
