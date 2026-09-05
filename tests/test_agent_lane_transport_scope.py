import io
import json
import sys
from types import SimpleNamespace

from sonder_runtime.bootstrap.native_mcp import run_native_mcp, native_tool_registry
from sonder_runtime.adapters.security.permission_policy import permission_policy


class Parents:
    token = 'dummy-transport-held-parent-proof'
    def open_model_parent(self, context):
        return {'parent_session_id': 'minted-root', 'parent_token': self.token,
                'revision': 1, 'expires_at': 9999999999}
    def verify_model_parent(self, parent, token, context):
        if (parent, token) != ('minted-root', self.token):
            raise PermissionError('invalid parent authority')


def native(application, arguments):
    messages = [{'jsonrpc': '2.0', 'id': 1, 'method': 'initialize', 'params': {
        'protocolVersion': '2.0', 'capabilities': {'tools': {}}}}]
    for index, args in enumerate(arguments, 2):
        messages.append({'jsonrpc': '2.0', 'id': index, 'method': 'tools/call',
                         'params': {'name': 'agent_lane', 'arguments': args}})
    output = io.StringIO()
    run_native_mcp(application, input_stream=io.StringIO('\n'.join(map(json.dumps, messages))+'\n'),
                   output_stream=output)
    return [json.loads(line) for line in output.getvalue().splitlines()][1:]


def test_shared_native_connection_has_no_implicit_parent_authority(monkeypatch, tmp_path):
    parents = Parents()
    application = SimpleNamespace(agent_lanes=lambda: parents,
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(tmp_path,))))
    captured = []
    monkeypatch.setattr(permission_policy, 'decide_for_caller', lambda *a, **k: SimpleNamespace(action='allow'))
    def dispatch(*args, **kwargs):
        captured.append(kwargs)
        return {'lanes': []}
    monkeypatch.setitem(sys.modules, 'sonder_runtime.interfaces.agent_lanes',
                        SimpleNamespace(dispatch_agent_lane_tool=dispatch))
    results = native(application, [
        {'action': 'open_parent', 'payload': {}},
        {'action': 'list', 'payload': {}},
        {'action': 'list', 'payload': {}, 'parent_session_id': 'minted-root', 'parent_token': parents.token},
        {'action': 'inspect', 'payload': {}, 'parent_session_id': 'someone-else', 'parent_token': parents.token},
    ])
    assert results[0]['result']['isError'] is False
    assert json.loads(results[0]['result']['output'])['parent_token'] == parents.token
    assert results[1]['result']['isError']
    assert captured == [{'parent_session_id': 'minted-root', 'bound_parent_session_id': 'minted-root'}]
    assert results[3]['result']['isError']
    assert parents.token not in json.dumps(results[1:])


def test_legacy_parent_proof_is_verified_and_not_in_gate_arguments(monkeypatch, tmp_path):
    import server
    parents = Parents()
    application = SimpleNamespace(agent_lanes=lambda: parents,
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(tmp_path,))))
    monkeypatch.setattr(server, '_application', lambda: application)
    captured = []
    monkeypatch.setattr(permission_policy, 'decide_for_caller',
                        lambda *a, **k: captured.append(k['arguments']) or SimpleNamespace(action='allow'))
    monkeypatch.setitem(sys.modules, 'sonder_runtime.interfaces.agent_lanes',
                        SimpleNamespace(dispatch_agent_lane_tool=lambda *a, **k: {'lanes': []}))
    assert json.loads(server.agent_lane('list', {}, 'minted-root', parents.token)) == {'lanes': []}
    assert parents.token not in repr(captured)
    assert captured[0]['parent_session_id'] == 'minted-root'


def test_parent_capability_is_redacted_from_activity_arguments_and_outputs():
    from sonder_runtime.adapters.observability.activity_tracker import _safe_args, _redact_text
    value = {'parent_session_id': 'minted-root', 'parent_token': Parents.token}
    assert Parents.token not in repr(_safe_args(value))
    assert Parents.token not in _redact_text(json.dumps(value))
