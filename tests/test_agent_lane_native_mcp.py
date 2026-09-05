from __future__ import annotations

import io
import json
import sys
from types import SimpleNamespace

from sonder_runtime.bootstrap.native_mcp import native_tool_registry, run_native_mcp
from sonder_runtime.adapters.security.permission_policy import permission_policy


def invoke(app, arguments):
    calls = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
            'protocolVersion': '2.0', 'capabilities': {'tools': {}}}},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {
            'name': 'agent_lane', 'arguments': arguments}},
    ]
    output = io.StringIO()
    run_native_mcp(app, input_stream=io.StringIO('\n'.join(map(json.dumps, calls))+'\n'),
                   output_stream=output)
    return [json.loads(line) for line in output.getvalue().splitlines()][-1]


def app(tmp_path):
    from tests.test_agent_lane_transport_scope import Parents
    return SimpleNamespace(config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(tmp_path,))),
                           agent_lanes=lambda: Parents())


def test_native_catalog_exposes_bounded_agent_control_schema():
    schema = native_tool_registry().require('agent_lane').input_schema
    assert schema['additionalProperties'] is False
    assert 'spawn' in schema['properties']['action']['enum']
    assert 'parent_token' in schema['properties']
    assert schema['properties']['payload']['maxProperties'] <= 32


def test_native_lane_calls_preserve_owner_scope_and_parent_binding(monkeypatch, tmp_path):
    captured = []
    def dispatch(service, action, payload, context, parent_session_id, parent_lane_id=None, **authority):
        assert authority['bound_parent_session_id'] == parent_session_id
        captured.append((service, action, payload, context, parent_session_id, parent_lane_id))
        return {'lane': {'lane_id': 'child-1'}}
    monkeypatch.setitem(sys.modules, 'sonder_runtime.interfaces.agent_lanes',
                        SimpleNamespace(dispatch_agent_lane_tool=dispatch))
    monkeypatch.setattr(permission_policy, 'decide_for_caller', lambda *a, **k: SimpleNamespace(action='allow'))
    result = invoke(app(tmp_path), {'action': 'spawn', 'payload': {'task': 'inspect'},
                                   'parent_session_id': 'minted-root',
                                   'parent_token': app(tmp_path).agent_lanes().token})
    assert result['result']['isError'] is False
    service, action, payload, context, session, parent = captured[0]
    assert (action, payload, session, parent) == ('spawn', {'task': 'inspect'}, 'minted-root', None)
    assert context.source == 'mcp' and context.principal_id == 'owner'
    assert context.workspace_roots == (tmp_path,)
    assert not context.cloud_allowed and not context.remote_ollama_allowed


def test_native_lane_denial_never_constructs_service(monkeypatch, tmp_path):
    application = app(tmp_path)
    def factory():
        raise AssertionError('denied command reached service')
    application.agent_lanes = factory
    monkeypatch.setattr(permission_policy, 'decide_for_caller', lambda *a, **k: SimpleNamespace(action='deny', reason='denied'))
    result = invoke(application, {'action': 'open_parent', 'payload': {}})
    assert result['result']['error'] == 'permission_denied'


def test_native_lane_tool_is_execution_class():
    import permission_modes
    assert permission_modes.risk_of('agent_lane') == 'execution'


def test_native_lane_scope_denial_has_stable_error(monkeypatch, tmp_path):
    def dispatch(*args, **kwargs):
        raise PermissionError('outside parent scope')
    monkeypatch.setitem(sys.modules, 'sonder_runtime.interfaces.agent_lanes',
                        SimpleNamespace(dispatch_agent_lane_tool=dispatch))
    monkeypatch.setattr(permission_policy, 'decide_for_caller', lambda *a, **k: SimpleNamespace(action='allow'))
    result = invoke(app(tmp_path), {'action': 'inspect', 'payload': {'lane_id': 'other'},
                                   'parent_session_id': 'minted-root',
                                   'parent_token': app(tmp_path).agent_lanes().token})
    assert result['result']['error'] == 'FORBIDDEN'


def test_legacy_mcp_exposes_same_lane_service_without_cloud_consent(monkeypatch, tmp_path):
    import server
    captured = []
    def dispatch(service, action, payload, context, parent_session_id, parent_lane_id=None, **authority):
        captured.append(context)
        assert parent_session_id == authority['bound_parent_session_id'] == 'minted-root'
        return {'lanes': []}
    monkeypatch.setitem(sys.modules, 'sonder_runtime.interfaces.agent_lanes',
                        SimpleNamespace(dispatch_agent_lane_tool=dispatch))
    monkeypatch.setattr(server, '_application', lambda: app(tmp_path))
    monkeypatch.setattr(permission_policy, 'decide_for_caller', lambda *a, **k: SimpleNamespace(action='allow'))
    result = json.loads(server.agent_lane('list', {}, 'minted-root', app(tmp_path).agent_lanes().token))
    assert result == {'lanes': []}
    assert captured[0].workspace_roots == (tmp_path,)
    assert not captured[0].cloud_allowed
