import json
from types import SimpleNamespace

import pytest

from tests.test_delegated_verification import lanes
from tests.test_lane_continuation import make_host, granted
from sonder_runtime.interfaces.standalone_agent_lanes import PreparedLaneCommand


def setup(lanes, controller=None):
    from sonder_runtime.adapters.host_terminal_projection import TerminalProjectionCodec
    from sonder_runtime.bootstrap.managed_standalone import ManagedStandaloneSession

    host, context, parent, current = make_host(lanes)
    host.projection_codec = TerminalProjectionCodec()
    controller = controller or SimpleNamespace(run_id='standalone-run')
    session = ManagedStandaloneSession(controller=controller, application=object(),
        host=host, context=context, host_conversation_id='host-task',
        private_paths=lambda: (lanes[3].parent / 'fleet.db',),
        model_writable_roots=lambda: (lanes[3],), approve=granted)
    return session, controller, host, current


def command(session, controller, action, payload):
    return PreparedLaneCommand(controller, json.dumps(dict(action=action, payload=payload,
        standalone_run_id=controller.run_id, principal_id=session.context.principal_id,
        workspace_roots=[str(p) for p in session.context.workspace_roots])))


def test_registration_and_bound_dispatch_use_no_retained_parent_token(lanes):
    session, controller, host, current = setup(lanes)
    result = session.dispatch(command(session, controller, 'spawn', dict(
        command_id='child', task='inspect', workspace_root=str(lanes[3]))))
    assert result['lane']['parent_session_id'] == session.parent_session_id
    assert session.report_metadata()['children'][0]['id'] == result['lane']['id']
    assert 'parent_token' not in vars(session)
    with pytest.raises(PermissionError):
        lanes[0].spawn(command_id='raw', parent_session_id=session.parent_session_id,
            task='raw', workspace_root=str(lanes[3]), context=session.context)
    session.close()


def test_foreign_command_or_changed_host_snapshot_cannot_dispatch(lanes):
    session, controller, host, current = setup(lanes)
    prepared = command(session, controller, 'list', {})
    with pytest.raises(PermissionError):
        session.dispatch(PreparedLaneCommand(object(), prepared.encoded))
    data = prepared.approval_arguments()
    data['standalone_run_id'] = 'another-run'
    with pytest.raises(PermissionError):
        session.dispatch(PreparedLaneCommand(controller, json.dumps(data)))
    session.close()


def test_close_detaches_without_cancelling_independent_child(lanes):
    session, controller, host, current = setup(lanes)
    child = session.dispatch(command(session, controller, 'spawn', dict(
        command_id='child', task='inspect', workspace_root=str(lanes[3]))))['lane']
    session.close()
    with lanes[1].transaction() as tx:
        assert tx.lane(child['id'])['status'] == 'queued'
    with pytest.raises(PermissionError):
        session.report_metadata()


def test_live_host_revocation_refuses_next_managed_command(lanes):
    from dataclasses import replace
    session, controller, host, current = setup(lanes)
    prepared = command(session, controller, 'list', {})
    current[0] = replace(current[0], workspace_roots=())
    with pytest.raises(PermissionError):
        session.dispatch(prepared)
    session.close()


def test_managed_verification_links_original_before_gate_and_publishes_receipt(lanes):
    import hashlib
    from tests.test_delegated_verification import _verifier
    from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
    from sonder_runtime.interfaces.standalone_agent_lanes import HostTerminalDraft

    session, controller, host, current = setup(lanes)
    child = session.dispatch(command(session, controller, 'spawn', dict(
        command_id='child', task='inspect', workspace_root=str(lanes[3]))))['lane']
    lanes[0].run_pending(child['id'], session.context)
    verifier, gateway, proofs = _verifier(lanes)
    execute = gateway.execute_check
    def checked(*args, **kwargs):
        execute(*args, **kwargs)
        for proof in proofs.values():
            proof['digest'] = hashlib.sha256(json.dumps(
                {key: value for key, value in proof.items() if key != 'digest'},
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    gateway.execute_check = checked
    ledger = HostObservationLedger(project_scope=str(lanes[3]))
    draft = HostTerminalDraft(ledger.seal(), 'original exact answer', 'NORMAL', ())
    approvals = []
    def approve(prepared, context):
        identity = session._bound.pending_verification()
        assert session._bound.terminal_projection(identity).output == draft.output
        approvals.append(prepared.verification_id)
        return granted()
    session._approve = approve
    factory = lambda app, service: verifier
    verdict = session.verify_delegated(draft, verifier_factory=factory)
    assert verdict.valid is True
    assert session.published_terminal.output == draft.output
    assert session.published_terminal.receipt.revision == 2
    assert session.verify_delegated(draft, verifier_factory=factory) == verdict
    assert gateway.calls == 1 and len(approvals) == 1
    (lanes[3] / 'changed-after-verification.txt').write_text('changed')
    assert session.verify_delegated(draft, verifier_factory=factory).valid is False
    assert session.published_terminal is None
    session.close()


def test_actual_controller_uses_managed_session_without_legacy_parent(lanes):
    from sonder_runtime.interfaces.standalone_agent_lanes import (
        managed_controller_factory_scope, controller_scope,
    )
    sessions = []
    def factory(controller, application):
        session, _, _, _ = setup(lanes, controller)
        sessions.append(session)
        return session
    with managed_controller_factory_scope(factory):
        with controller_scope(lambda: object()) as controller:
            result = controller.execute({'action': 'spawn', 'payload': {
                'command_id': 'actual-controller', 'task': 'inspect',
                'workspace_root': str(lanes[3]),
            }})
            assert controller._parent is None
            assert controller.report_metadata()['parent_session_id'] == result['lane']['parent_session_id']
            assert controller._context is sessions[0].context
    with pytest.raises(PermissionError):
        sessions[0].require_current()
