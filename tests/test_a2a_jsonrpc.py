from __future__ import annotations

import json

from sonder_runtime.interfaces.a2a.jsonrpc import A2AJsonRpcLimits, A2AJsonRpcTransport


def _request(method, params=None, request_id=1):
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}


def test_a2a_jsonrpc_dispatches_supported_method_and_preserves_id():
    calls = []

    def handler(method, params):
        calls.append((method, dict(params)))
        return {"task": {"id": "t-1", "status": {"state": "TASK_STATE_WORKING"}}}

    response = A2AJsonRpcTransport(handler).handle(_request("SendMessage", {"message": {"parts": []}}))
    assert response["id"] == 1
    assert response["result"]["task"]["id"] == "t-1"
    assert calls[0][0] == "SendMessage"


def test_a2a_jsonrpc_rejects_unknown_methods_and_malformed_envelopes():
    transport = A2AJsonRpcTransport(lambda *_: {})
    assert transport.handle(_request("DeleteEverything"))["error"]["code"] == -32601
    assert transport.handle({"jsonrpc": "1.0", "id": 1, "method": "GetTask"})["error"]["code"] == -32600
    assert transport.handle(_request("GetTask", request_id=True))["error"]["code"] == -32600
    assert transport.handle("not-json")["error"]["code"] == -32600


def test_a2a_jsonrpc_contains_handler_errors_without_details():
    def handler(*_):
        raise RuntimeError("credential should not cross the boundary")

    response = A2AJsonRpcTransport(handler).handle(_request("GetTask"))
    assert response["error"]["code"] == -32603
    assert "credential" not in json.dumps(response)


def test_a2a_jsonrpc_bounds_request_and_response_payloads():
    transport = A2AJsonRpcTransport(
        lambda *_: {"value": "x" * 100},
        limits=A2AJsonRpcLimits(max_request_bytes=64, max_response_bytes=64),
    )
    assert transport.handle(_request("GetTask", {"value": "x" * 100}))["error"]["code"] == -32600
    assert transport.handle(_request("GetTask"))["error"]["code"] == -32603
