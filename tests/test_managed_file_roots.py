import pytest

from sonder_runtime.adapters.filesystem import file_ops


def test_managed_roots_attenuate_defaults_extra_reach_and_bypass(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    outside = tmp_path / 'outside'
    project.mkdir()
    outside.mkdir()
    monkeypatch.setattr(file_ops, 'workspace_root', lambda: tmp_path)
    with file_ops.managed_root_scope(lambda: (project,)):
        assert file_ops.allowed_roots(str(outside)) == [project.resolve()]
        with file_ops.reach_scope(lambda: str(outside)):
            assert file_ops.resolve_path(str(project / 'new'), bypass=True) == project / 'new'
            with pytest.raises(PermissionError):
                file_ops.resolve_path(str(outside / 'new'), bypass=True, extra_roots=str(outside))
    assert file_ops.resolve_path(str(outside / 'new')) == outside / 'new'


def test_managed_scope_is_live_and_nested_scopes_cannot_expand(tmp_path, monkeypatch):
    project = tmp_path / 'project'
    project.mkdir()
    monkeypatch.setattr(file_ops, 'workspace_root', lambda: tmp_path)
    roots = [project]
    with file_ops.managed_root_scope(lambda: tuple(roots)):
        with file_ops.managed_root_scope(lambda: (tmp_path,)):
            assert file_ops.allowed_roots() == [project.resolve()]
        roots.clear()
        with pytest.raises(PermissionError):
            file_ops.resolve_path(str(project / 'new'), bypass=True)


def test_managed_scope_provider_failure_refuses_access(tmp_path, monkeypatch):
    monkeypatch.setattr(file_ops, 'workspace_root', lambda: tmp_path)

    def unavailable():
        raise PermissionError('host selection revoked')

    with file_ops.managed_root_scope(unavailable):
        with pytest.raises(PermissionError, match='revoked'):
            file_ops.resolve_path(str(tmp_path / 'new'), bypass=True)


def test_default_terminal_outputs_are_protected_outside_managed_scope():
    from sonder_runtime.adapters.security.control_plane_paths import live_control_plane_inventory
    from sonder_runtime.platform.paths import default_home
    assert live_control_plane_inventory().protects(default_home() / 'terminal-output' / 'outputs.db')
