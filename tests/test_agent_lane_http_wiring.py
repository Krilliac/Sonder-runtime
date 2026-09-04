from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from sonder_runtime.interfaces.http import serve
from sonder_runtime.adapters.web import lifecycle


@pytest.fixture
def lane_http(tmp_path, monkeypatch):
    monkeypatch.setenv('SONDER_HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('SONDER_OPERATIONS_DB', str(tmp_path / 'operations.db'))
    lifecycle.reset_for_tests()
    server = ThreadingHTTPServer(('127.0.0.1', 0), serve.Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{server.server_port}'
    server.shutdown()
    server.server_close()
    thread.join(timeout=3)
    lifecycle.reset_for_tests()


def request(base, path='/v1/agent-lanes', payload=None):
    req = urllib.request.Request(base + path,
        data=None if payload is None else json.dumps(payload).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=5) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read())


def auth(monkeypatch, *, authorized=True, account=None):
    monkeypatch.setattr(serve.Handler, '_request_auth_context', lambda self: {
        'authorized': authorized, 'mode': 'account' if account else 'api-key',
        'api_key': not account, 'account': account,
    })


@pytest.mark.parametrize('payload', [None, {'prompt': 'work'}])
def test_agent_routes_require_auth_before_service_access(lane_http, monkeypatch, payload):
    auth(monkeypatch, authorized=False)
    def forbidden_factory():
        pytest.fail('unauthenticated request constructed application')
    monkeypatch.setattr('sonder_runtime.bootstrap.app.default_app', forbidden_factory)
    status, _ = request(lane_http, payload=payload)
    assert status == 401


def test_missing_lane_service_is_unavailable(lane_http, monkeypatch):
    auth(monkeypatch)
    monkeypatch.setattr('sonder_runtime.bootstrap.app.default_app', lambda: SimpleNamespace())
    status, body = request(lane_http)
    assert status == 503
    assert body['error']['code'] == 'DEPENDENCY_UNAVAILABLE'


def install_dispatch(monkeypatch, tmp_path, callback):
    # Exercise the real HTTP auth/framing/dispatch boundary independently of
    # the asynchronous lane scheduler, whose service tests own execution.
    monkeypatch.setitem(sys.modules, 'sonder_runtime.interfaces.http.facades.agent_lanes',
                        SimpleNamespace(dispatch_agent_lane_route=callback))
    monkeypatch.setattr('sonder_runtime.bootstrap.app.default_app', lambda: SimpleNamespace(
        agent_lanes=lambda: 'service',
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(str(tmp_path),))),
    ))


def test_dispatch_uses_authenticated_owner_and_configured_scope(lane_http, monkeypatch, tmp_path):
    auth(monkeypatch)
    captured = []
    def dispatch(service, method, path, payload, query, context):
        captured.append((service, method, path, payload, query, context))
        return SimpleNamespace(body={'lanes': []}, status_code=200)
    install_dispatch(monkeypatch, tmp_path, dispatch)
    status, body = request(lane_http, '/v1/agent-lanes?limit=20')
    assert status == 200 and body == {'lanes': []}
    service, method, path, _, query, context = captured[0]
    assert (service, method, path, query) == ('service', 'GET', '/v1/agent-lanes', {'limit': '20'})
    assert context.principal_id == 'owner'
    assert context.workspace_roots == (tmp_path,)
    assert not context.cloud_allowed and not context.remote_ollama_allowed


def test_account_owner_cannot_alias_local_owner_or_inherit_workspace(lane_http, monkeypatch, tmp_path):
    auth(monkeypatch, account={'username': 'owner', 'role': 'user'})
    captured = []
    def dispatch(service, method, path, payload, query, context):
        captured.append(context)
        return SimpleNamespace(body={'lanes': []}, status_code=200)
    install_dispatch(monkeypatch, tmp_path, dispatch)
    assert request(lane_http)[0] == 200
    assert captured[0].principal_id.startswith('account:')
    assert captured[0].principal_id != 'owner'
    assert captured[0].workspace_roots == ()


def test_duplicate_query_values_do_not_reach_lane_dispatch(lane_http, monkeypatch, tmp_path):
    auth(monkeypatch)
    def dispatch(*args, **kwargs):
        pytest.fail('ambiguous query reached service')
    install_dispatch(monkeypatch, tmp_path, dispatch)
    assert request(lane_http, '/v1/agent-lanes?limit=1&limit=2')[0] == 400


def test_unexpected_lane_errors_do_not_expose_exception_text(lane_http, monkeypatch, tmp_path):
    auth(monkeypatch)
    def dispatch(*args, **kwargs):
        raise RuntimeError('private provider detail')
    install_dispatch(monkeypatch, tmp_path, dispatch)
    status, body = request(lane_http)
    assert status == 503
    assert 'private provider detail' not in json.dumps(body)
