"""Real one-shot approval ledger coverage at the three lane entrypoints."""
import sys
from types import SimpleNamespace

import pytest
import permission_modes as pm
from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
from sonder_runtime.application.context import local_owner_context
from sonder_runtime.interfaces.http import serve
from tests.test_agent_lane_native_mcp import app, invoke


@pytest.fixture
def ledger(tmp_path, monkeypatch):
    store = ApprovalLedger(tmp_path / 'lane-approvals.db')
    monkeypatch.setattr(pm, '_approval_ledger', lambda: store)
    monkeypatch.setattr(pm, '_rule_lookup', lambda _: None)
    monkeypatch.setattr(pm, 'current_mode', lambda: pm.MANUAL)
    pm.reset_unattended_for_tests()
    yield store
    pm.forget_spent_approval()
    pm.reset_unattended_for_tests()


@pytest.mark.parametrize('surface', ['native', 'legacy', 'registered'])
def test_exact_mcp_arguments_spend_one_approval_only(surface, ledger, tmp_path, monkeypatch):
    import server
    calls = []
    monkeypatch.setitem(sys.modules, 'sonder_runtime.interfaces.agent_lanes', SimpleNamespace(
        dispatch_agent_lane_tool=lambda *a, **k: calls.append(k) or {'ok': True}))
    application = app(tmp_path)
    monkeypatch.setattr(server, '_application', lambda: application)
    args = {'action':'spawn', 'payload':{'task':'bounded work'}, 'parent_session_id':'minted-root',
            'parent_token': application.agent_lanes().token}
    def run(arguments):
        if surface == 'native':
            return not invoke(application, arguments)['result']['isError']
        try:
            if surface == 'registered':
                import asyncio
                result = asyncio.run(server.mcp.call_tool('agent_lane', arguments))
                assert not result.is_error
            else:
                server.agent_lane(**arguments)
            return True
        except Exception as error:
            from sonder_runtime.domain.common.errors import Forbidden
            from mcp.server.mcpserver.exceptions import ToolError
            assert isinstance(error, (Forbidden, ToolError))
            return False
    assert not run(args)
    from sonder_runtime.interfaces.agent_lane_entrypoint import lane_approval_arguments
    context = local_owner_context(correlation_id='test', source='mcp', workspace_roots=(tmp_path,))
    safe = lane_approval_arguments(application, context, args)
    grant = ledger.issue('agent_lane', pm.call_digest('agent_lane', safe), approver='test')
    assert not run({**args, 'payload': {'task':'different work'}})
    # Legacy's omitted optional argument defaults to empty; it must hash the
    # same invocation as native MCP's omitted optional argument.
    assert run(args)
    assert ledger.get(grant.nonce).spent
    assert not run(args)
    assert len(calls) == 1


def test_http_mutations_bind_approval_to_request_and_principal(ledger, tmp_path, monkeypatch):
    auth = {'authorized':True, 'mode':'api-key', 'api_key':True, 'account':None}
    calls, responses = [], []
    handler = SimpleNamespace(path='/v1/agent-lanes',
        _request_auth_context=lambda: auth,
        _correlation=lambda:'test-lane',
        _send_json_payload=lambda body, status=200: responses.append((status,body)),
        _send_auth_error=lambda: responses.append((401,{})),
        _send_not_found=lambda: responses.append((404,{})))
    application = app(tmp_path)
    monkeypatch.setattr('sonder_runtime.bootstrap.app.default_app', lambda: application)
    monkeypatch.setattr(serve.sonder_lifecycle, 'get', lambda: SimpleNamespace(
        operation_context=lambda *a: local_owner_context(correlation_id='test', source='http')))
    monkeypatch.setitem(sys.modules, 'sonder_runtime.interfaces.http.facades.agent_lanes', SimpleNamespace(
        dispatch_agent_lane_route=lambda *a: calls.append(a) or SimpleNamespace(body={'ok':True},status_code=202)))
    payload = {'command_id':'one', 'task':'bounded work'}
    def run(method='POST', body=payload):
        serve.Handler._handle_agent_lane_request(handler, method, '/v1/agent-lanes', body)
        return responses[-1][0]
    assert run() == 403
    arguments = {'method':'POST','path':'/v1/agent-lanes','payload':payload,'query':{},
                 'principal_id':'owner','workspace_roots':[str(tmp_path.resolve())]}
    grant = ledger.issue('agent_lane', pm.call_digest('agent_lane',arguments), approver='test')
    assert run(body={**payload,'task':'different work'}) == 403
    auth.update(mode='account',api_key=False,account={'username':'other','role':'user'})
    assert run() == 403
    assert not ledger.get(grant.nonce).spent
    auth.update(mode='api-key',api_key=True,account=None)
    assert run() == 202
    assert ledger.get(grant.nonce).spent
    assert run() == 403
    assert len(calls) == 1
    monkeypatch.setattr(pm, 'current_mode', lambda: pm.PLAN)
    assert run('GET', {}) == 202
    assert run() == 403
