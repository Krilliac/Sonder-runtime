from types import SimpleNamespace
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
