from types import SimpleNamespace
from pathlib import Path
import json

import pytest
import permission_modes

from tests.test_delegated_verification import lanes
from sonder_runtime.adapters.filesystem import file_ops
from sonder_runtime.adapters.security.control_plane_paths import ControlPlanePaths
from sonder_runtime.bootstrap.repl_managed import run_managed_repl_work
from sonder_runtime.interfaces.standalone_agent_lanes import controller_scope


@pytest.mark.parametrize('entry', ['composition', 'repl'])
def test_composition_registers_selected_session_and_closes_without_bearer(
        lanes, tmp_path_factory, monkeypatch, entry):
    project = tmp_path_factory.mktemp('managed-project')
    monkeypatch.setattr(file_ops, 'workspace_root', lambda: project)
    application = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(project,)),
                               ollama=SimpleNamespace(allow_remote=False)),
        agent_lanes=lambda: lanes[0],
        private_source_paths=(lanes[1].path,),
    )
    captured = []

    def run():
        with controller_scope(lambda: application, project=str(project)) as controller:
            controller.require_current()
            captured.append(controller._managed_session)
            assert controller._parent is None
            assert controller._managed_session._host_id == 'repl-session:1111111111111111'
            assert file_ops.allowed_roots() == [project]
            with pytest.raises(PermissionError):
                file_ops.resolve_path(str(lanes[1].path), bypass=True)
            return controller._managed_session.report_metadata()

    if entry == 'repl':
        import server
        from sonder_runtime.interfaces.repl import repl
        monkeypatch.setattr(repl, '_legacy_runtime', None)
        repl.configure_legacy_runtime(server)
        monkeypatch.setattr(server, '_application', lambda: application)
        monkeypatch.setattr(server, 'workbench_agent', lambda **arguments: run())
        result = repl._run_session_work('1111111111111111', host_project='test',
                                        project=str(project), prompt='inspect')
    else:
        result = run_managed_repl_work(
            application=application, session_id='1111111111111111', project=str(project),
            get_session=lambda value: {'session_id': value}, run=run,
            permission_engine=permission_modes,
            additional_paths=lambda: ControlPlanePaths(databases=(lanes[1].path,)),
        )
    assert result['continuation_id']
    with pytest.raises(PermissionError):
        captured[0].require_current()


def test_private_inventory_overlap_refuses_before_lane_provider(tmp_path):
    application = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(tmp_path,))),
        agent_lanes=lambda: pytest.fail('provider initialized before preflight'),
    )
    with pytest.raises(PermissionError):
        run_managed_repl_work(
            application=application, session_id='1111111111111111', project=str(tmp_path),
            get_session=lambda value: {'session_id': value}, run=lambda: pytest.fail('run'),
            permission_engine=permission_modes,
            additional_paths=lambda: ControlPlanePaths(databases=(tmp_path / 'private.db',)),
        )


def test_private_state_in_other_configured_project_refuses_before_provider(tmp_path):
    selected = tmp_path / 'selected'
    other = tmp_path / 'other'
    selected.mkdir()
    other.mkdir()
    application = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(selected, other))),
        agent_lanes=lambda: pytest.fail('provider initialized with exposed control state'),
    )
    with pytest.raises(PermissionError):
        run_managed_repl_work(
            application=application, session_id='1111111111111111', project=str(selected),
            get_session=lambda value: {'session_id': value}, run=lambda: pytest.fail('run'),
            permission_engine=permission_modes,
            additional_paths=lambda: ControlPlanePaths(files=(other / 'secrets.env',)),
        )


def test_loaded_toml_and_real_application_support_managed_repl_work(
        tmp_path, tmp_path_factory, monkeypatch):
    import server
    from sonder_runtime.bootstrap.app import build_application
    from sonder_runtime.platform.config import load_config
    from sonder_runtime.interfaces.repl import repl

    project = tmp_path_factory.mktemp('configured-project')
    source = tmp_path / 'sonder.toml'
    source.write_text('[state]\nworkspace_roots = ' + json.dumps([str(project)]) + '\n',
                      encoding='utf-8')
    application = build_application(config=load_config(source))
    monkeypatch.setattr(file_ops, 'workspace_root', lambda: project)
    monkeypatch.setattr(server, '_application', lambda: application)
    monkeypatch.setattr(repl, '_legacy_runtime', None)
    repl.configure_legacy_runtime(server)

    def work(**arguments):
        with controller_scope(server._application, project=str(project)) as controller:
            controller.require_current()
            return controller._managed_session.report_metadata()

    monkeypatch.setattr(server, 'workbench_agent', work)
    try:
        result = repl._run_session_work('2222222222222222', host_project='configured',
                                        project=str(project), prompt='inspect')
        assert result['continuation_id']
        assert str(source.resolve()) in application.private_source_paths
        store = application.agent_lanes().store
        with store.transaction() as transaction:
            before = [tuple(row) for row in transaction.conn.execute(
                'SELECT position,data FROM agent_lane_continuations ORDER BY position')]
        monkeypatch.setattr(server, 'workbench_agent', lambda **arguments: pytest.fail('inspection ran work'))
        monkeypatch.setattr(permission_modes, 'decide', lambda *args, **kwargs: pytest.fail('inspection spent approval'))
        rendered = repl._recovery_command('2222222222222222', str(project), '')
        assert result['continuation_id'] in rendered
        assert 'inspection only' in rendered
        with store.transaction() as transaction:
            after = [tuple(row) for row in transaction.conn.execute(
                'SELECT position,data FROM agent_lane_continuations ORDER BY position')]
        assert after == before
    finally:
        application.close_providers(timeout=5)


@pytest.mark.parametrize('argument', ['resume', '-1', '1.2', '９', str(2**63)])
def test_recovery_rejects_invalid_cursor_before_database(argument, monkeypatch):
    import server
    from sonder_runtime.interfaces.repl import repl
    monkeypatch.setattr(repl, '_legacy_runtime', None)
    repl.configure_legacy_runtime(server)
    monkeypatch.setattr(server, '_open_db', lambda: pytest.fail('invalid cursor reached storage'))
    result = repl._recovery_command('2222222222222222', '', argument)
    assert 'Usage:' in result or 'outside' in result


@pytest.mark.parametrize('entry', ['composition', 'console'])
def test_explicit_recovery_preserves_original_and_spends_separate_approvals(
        lanes, tmp_path_factory, monkeypatch, entry):
    import hashlib
    from sonder_runtime.bootstrap.repl_managed import ReplRecoveryRequest
    from sonder_runtime.adapters.security.approval_ledger import ApprovalLedger
    from sonder_runtime.adapters.agent_terminal_evidence import HostObservationLedger
    from sonder_runtime.interfaces.standalone_agent_lanes import HostTerminalDraft
    from tests.test_managed_standalone_session import command
    from tests.test_delegated_verification import _verifier

    project = tmp_path_factory.mktemp('resume-project')
    monkeypatch.setattr(file_ops, 'workspace_root', lambda: project)
    application = SimpleNamespace(
        config=SimpleNamespace(state=SimpleNamespace(workspace_roots=(project,)),
                               ollama=SimpleNamespace(allow_remote=False)),
        agent_lanes=lambda: lanes[0])
    ledger = ApprovalLedger(Path(lanes[1].path).parent / 'explicit-approvals.db')
    engine = SimpleNamespace(
        approval_ledger=lambda: ledger, call_digest=permission_modes.call_digest,
        decide=lambda tool, **kwargs: permission_modes.decide(
            tool, mode='manual', rule_lookup=lambda _: None, **kwargs))
    verifier, gateway, proofs = _verifier((*lanes[:3], project, *lanes[4:]))
    execute = gateway.execute_check

    def checked(*args, **kwargs):
        execute(*args, **kwargs)
        for proof in proofs.values():
            proof['digest'] = hashlib.sha256(json.dumps(
                {key: value for key, value in proof.items() if key != 'digest'},
                sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    gateway.execute_check = checked
    original = []

    def work():
        with controller_scope(lambda: application, project=str(project)) as controller:
            controller.require_current()
            session = controller._managed_session
            child = session.dispatch(command(session, controller, 'spawn', dict(
                command_id='child', task='inspect', workspace_root=str(project))))['lane']
            lanes[0].run_pending(child['id'], session.context)
            draft = HostTerminalDraft(HostObservationLedger(project_scope=str(project)).seal(),
                                      'original verified answer', 'NORMAL', ())
            assert not session.verify_delegated(draft, verifier_factory=lambda *args: verifier).valid
            identity = session._bound.pending_verification()
            prepared = session._bound.prepared_verification(identity)
            original.append((identity, prepared))

    arguments = dict(application=application, session_id='3333333333333333',
                     project=str(project), get_session=lambda value: {'session_id': value},
                     permission_engine=engine, ledger=ledger,
                     additional_paths=lambda: ControlPlanePaths(databases=(lanes[1].path, ledger.path)))
    run_managed_repl_work(**arguments, run=work)
    identity, prepared = original[0]
    request = ReplRecoveryRequest(identity.continuation_id, 'resume-original')
    if entry == 'console':
        import re
        import server
        from sonder_runtime.interfaces.repl import repl
        application.private_source_paths = (lanes[1].path, ledger.path)
        monkeypatch.setattr(server, '_application', lambda: application)
        monkeypatch.setattr(server, 'permission_modes', engine)
        monkeypatch.setattr(server, '_standalone_verifier_factory', lambda *args: verifier)
        monkeypatch.setattr(server, 'workbench_agent', lambda **kwargs: pytest.fail('console recovery ran model'))
        monkeypatch.setattr(repl, '_legacy_runtime', None)
        repl.configure_legacy_runtime(server)
        connection = server._open_db()
        try:
            repl.memory_store.touch_session(connection, '3333333333333333')
        finally:
            connection.close()
        recover = lambda: repl._recovery_command('3333333333333333', str(project),
                                                 'resume ' + request.continuation_id + ' ' + request.command_id)
        pending = recover()
        assert 'ATTACHMENT_APPROVAL_PENDING' in pending
        call_id = re.search(r'Pending approval: ([0-9a-f]{16})', pending).group(1)
    else:
        recover = lambda: run_managed_repl_work(**arguments, run=lambda: pytest.fail('recovery ran model'),
                                                 recovery_request=request, verifier_factory=lambda *args: verifier)
        pending = recover()
        assert pending.code == 'ATTACHMENT_APPROVAL_PENDING'
        call_id = pending.approval_call_id
    attachment = ledger.resolve_call(call_id)
    attachment_nonce = ledger.issue(attachment.tool, attachment.digest, approver='operator').nonce
    verification = ledger.resolve_call(permission_modes.call_digest('workspace_run', prepared.approval_payload()))
    verification_nonce = ledger.issue(verification.tool, verification.digest, approver='operator').nonce
    result = recover()
    if entry == 'console':
        assert 'Recovery: VERIFIED' in result and 'original verified answer' in result
    else:
        assert result.code == 'VERIFIED' and result.output == 'original verified answer'
    assert ledger.get(attachment_nonce).spent and ledger.get(verification_nonce).spent
    assert gateway.calls == 1
