import pytest

import file_ops
import server


def test_repo_local_roots_file_is_not_trusted(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    workspace.mkdir()
    state.mkdir()
    outside.mkdir()
    target = outside / "secret.txt"
    target.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.trilobite_paths, "default_home", lambda: state)
    monkeypatch.delenv("TRILOBITE_FILE_ROOTS_FILE", raising=False)
    (workspace / file_ops.DEFAULT_ROOTS_FILE).write_text(str(outside), encoding="utf-8")
    with pytest.raises(PermissionError):
        file_ops.read_file(str(target))


@pytest.mark.parametrize(
    "name",
    [
        "file_roots.local", "permissions.json", "workflows.json",
        "emotion_vectors.json", "system_profile.md", "memory.db",
        "memory.db-wal", ".credentials.json", "server.py",
    ],
)
def test_control_plane_requires_developer(monkeypatch, tmp_path, name):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    target = tmp_path / name
    target.write_text("before", encoding="utf-8")
    with pytest.raises(PermissionError, match="authenticated developer token"):
        file_ops.write_file(str(target), "after", mode="overwrite")
    with pytest.raises(PermissionError, match="authenticated developer token"):
        file_ops.edit_file(str(target), "before", "after")
    with pytest.raises(PermissionError, match="authenticated developer token"):
        file_ops.delete_path(
            str(target), dry_run=False, confirm="DELETE %s" % target.resolve()
        )
    assert target.read_text(encoding="utf-8") == "before"


def test_developer_flag_can_edit_control_plane(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    target = tmp_path / "permissions.json"
    target.write_text("before", encoding="utf-8")
    result = file_ops.edit_file(
        str(target), "before", "after", developer_authorized=True
    )
    assert result["replacements"] == 1
    assert target.read_text(encoding="utf-8") == "after"


def test_nested_project_python_remains_editable(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    target = tmp_path / "project" / "module.py"
    target.parent.mkdir()
    target.write_text("before", encoding="utf-8")
    file_ops.edit_file(str(target), "before", "after")
    assert target.read_text(encoding="utf-8") == "after"


def test_approval_code_cannot_edit_control_plane(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    target = root / "permissions.json"
    target.write_text("before", encoding="utf-8")
    monkeypatch.setattr(server.file_ops, "workspace_root", lambda: root)
    monkeypatch.setenv("TRILOBITE_FILE_APPROVAL_CODE", "let-me")
    out = server.file_edit(
        str(target), "before", "after", approval="let-me"
    )
    assert out.startswith("ERROR:")
    assert target.read_text(encoding="utf-8") == "before"


def test_recursive_delete_rejects_allowed_root(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    workspace.mkdir()
    state.mkdir()
    marker = workspace / "ordinary.txt"
    marker.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.trilobite_paths, "default_home", lambda: state)

    with pytest.raises(PermissionError, match="allowed/configured root"):
        file_ops.delete_path(
            str(workspace),
            recursive=True,
            dry_run=False,
            confirm="DELETE %s" % workspace.resolve(),
        )

    assert marker.read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("protected_name", ["permissions.json", ".env", "memory.db"])
def test_recursive_delete_rejects_protected_descendant(
    monkeypatch, tmp_path, protected_name
):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    target = workspace / "project"
    nested = target / "nested"
    nested.mkdir(parents=True)
    state.mkdir()
    protected = nested / protected_name
    protected.write_text("keep", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.trilobite_paths, "default_home", lambda: state)

    with pytest.raises(PermissionError, match="protected control state"):
        file_ops.delete_path(
            str(target),
            recursive=True,
            dry_run=False,
            confirm="DELETE %s" % target.resolve(),
        )

    assert protected.exists()


def test_recursive_delete_rejects_reparse_descendant(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    target = workspace / "project"
    junction = target / "junction"
    junction.mkdir(parents=True)
    state.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.trilobite_paths, "default_home", lambda: state)
    original = file_ops._is_reparse_point
    monkeypatch.setattr(
        file_ops,
        "_is_reparse_point",
        lambda path: path == junction or original(path),
    )

    with pytest.raises(PermissionError, match="symlink or junction"):
        file_ops.delete_path(
            str(target),
            recursive=True,
            dry_run=False,
            confirm="DELETE %s" % target.resolve(),
        )

    assert target.exists()


def test_developer_can_recursively_delete_tree_with_protected_descendant(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    target = workspace / "project"
    target.mkdir(parents=True)
    state.mkdir()
    (target / "permissions.json").write_text("ok", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.trilobite_paths, "default_home", lambda: state)

    result = file_ops.delete_path(
        str(target),
        recursive=True,
        dry_run=False,
        confirm="DELETE %s" % target.resolve(),
        developer_authorized=True,
    )

    assert result["deleted"] is True
    assert not target.exists()
