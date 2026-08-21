from __future__ import annotations

from sonder_runtime.interfaces.http.facades.a2a_jsonrpc import dispatch_a2a_jsonrpc_route


def _request(method="GetTask"):
    return {"jsonrpc": "2.0", "id": 1, "method": method, "params": {}}


def test_a2a_http_route_delegates_only_to_explicit_handler():
    calls = []

    def handler(method, params):
        calls.append((method, params))
        return {"task": {"id": "task-1"}}

    result = dispatch_a2a_jsonrpc_route(handler, "POST", "/a2a", _request())
    assert result.status_code == 200
    assert result.body["result"]["task"]["id"] == "task-1"
    assert calls == [("GetTask", {})]


def test_a2a_http_route_is_truthful_when_handler_is_not_configured():
    result = dispatch_a2a_jsonrpc_route(None, "POST", "/a2a", _request())
    assert result.status_code == 503
    assert result.body["error"]["code"] == "A2A_UNAVAILABLE"


def test_a2a_http_route_rejects_wrong_method_and_path():
    assert dispatch_a2a_jsonrpc_route(lambda *_: {}, "GET", "/a2a", _request()).status_code == 405
    assert dispatch_a2a_jsonrpc_route(lambda *_: {}, "POST", "/other", _request()) is None
