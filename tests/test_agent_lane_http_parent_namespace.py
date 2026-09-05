"""An HTTP parent alias cannot attach lane events to unrelated sessions."""
from types import SimpleNamespace

from tests.test_agent_lane_http_wiring import lane_http, auth, install_dispatch, request


def test_parent_alias_is_scoped_and_roundtrips_without_rehash(lane_http, monkeypatch, tmp_path):
    auth(monkeypatch)
    seen = []
    def dispatch(service, method, path, payload, query, context):
        parent = payload.get('parent_session_id') or query.get('parent_session_id')
        seen.append(parent)
        return SimpleNamespace(body={'parent_session_id': parent}, status_code=200)
    install_dispatch(monkeypatch, tmp_path, dispatch)
    raw = 'existing-chat-session'
    status, body = request(lane_http, '/v1/agent-lanes?parent_session_id=' + raw)
    assert status == 200
    owner_parent = body['parent_session_id']
    assert owner_parent != raw
    assert request(lane_http, '/v1/agent-lanes?parent_session_id=' + owner_parent)[1] == body
    auth(monkeypatch, account={'username': 'other', 'role': 'user'})
    account_parent = request(lane_http, '/v1/agent-lanes?parent_session_id=' + raw)[1]['parent_session_id']
    assert account_parent not in {raw, owner_parent}
    assert request(lane_http, '/v1/agent-lanes?parent_session_id=' + owner_parent)[1]['parent_session_id'] != owner_parent


def test_invalid_parent_type_is_rejected_before_dispatch(lane_http, monkeypatch, tmp_path):
    auth(monkeypatch)
    def dispatch(*args):
        raise AssertionError('invalid parent reached service')
    install_dispatch(monkeypatch, tmp_path, dispatch)
    assert request(lane_http, payload={'parent_session_id': ['not-a-session']})[0] == 400
