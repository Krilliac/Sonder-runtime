from types import SimpleNamespace

import pytest

from sonder_runtime.application.context import local_owner_context


class Parents:
    def __init__(self):
        self.verifications = []
    def verify_model_parent(self, parent_id, token, context):
        if (parent_id, token) != ('parent-1', 'test-parent-proof-long-enough'):
            raise PermissionError('parent authority is invalid')
        self.verifications.append((parent_id, context.principal_id))


def test_approval_arguments_verify_proof_but_never_include_it(tmp_path):
    from sonder_runtime.interfaces.agent_lane_entrypoint import lane_approval_arguments
    parents = Parents()
    application = SimpleNamespace(agent_lanes=lambda: parents)
    context = local_owner_context(correlation_id='test', workspace_roots=(tmp_path,))
    safe = lane_approval_arguments(application, context, {
        'action': 'inspect', 'payload': {'lane_id': 'child'},
        'parent_session_id': 'parent-1', 'parent_token': 'test-parent-proof-long-enough',
    })
    assert parents.verifications == [('parent-1', 'owner')]
    assert 'test-parent-proof-long-enough' not in repr(safe)
    assert 'parent_token' not in safe
    assert safe['parent_session_id'] == 'parent-1'
    assert safe['principal_id'] == 'owner'


def test_wrong_parent_proof_cannot_reach_approval_gate(tmp_path):
    from sonder_runtime.interfaces.agent_lane_entrypoint import lane_approval_arguments
    application = SimpleNamespace(agent_lanes=Parents)
    with pytest.raises(PermissionError):
        lane_approval_arguments(application, local_owner_context(correlation_id='test'), {
            'action': 'inspect', 'payload': {}, 'parent_session_id': 'other',
            'parent_token': 'test-parent-proof-long-enough',
        })


def test_open_parent_cannot_attach_existing_identity():
    from sonder_runtime.interfaces.agent_lane_entrypoint import lane_approval_arguments
    application = SimpleNamespace(agent_lanes=Parents)
    with pytest.raises(ValueError):
        lane_approval_arguments(application, local_owner_context(correlation_id='test'), {
            'action': 'open_parent', 'payload': {}, 'parent_session_id': 'existing',
        })
