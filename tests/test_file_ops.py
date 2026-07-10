import os

import pytest

import file_ops


def test_write_read_edit_delete_inside_workspace(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    path = "notes/demo.txt"

    wrote = file_ops.write_file(path, "hello world")
    read = file_ops.read_file(path)
    edited = file_ops.edit_file(path, "world", "there")
    dry = file_ops.delete_path(path)
    deleted = file_ops.delete_path(path, dry_run=False, confirm=dry["required_confirm"])

    assert wrote["bytes"] == len("hello world")
    assert read["text"] == "hello world"
    assert edited["replacements"] == 1
    assert dry["deleted"] is False
    assert deleted["deleted"] is True


def test_outside_workspace_rejected_without_bypass(monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside.txt"
    root.mkdir()
    outside.write_text("secret", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)

    with pytest.raises(PermissionError):
        file_ops.read_file(str(outside))


def test_extra_roots_only_apply_with_bypass(monkeypatch, tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    target = outside / "ok.txt"
    target.write_text("ok", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: root)

    with pytest.raises(PermissionError):
        file_ops.read_file(str(target), extra_roots=str(outside), bypass=False)

    assert file_ops.read_file(str(target), extra_roots=str(outside), bypass=True)["text"] == "ok"


def test_hot_roots_file_allows_explicit_read_root(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    repo = tmp_path / "repo"
    workspace.mkdir()
    repo.mkdir()
    target = repo / "README.md"
    target.write_text("ground truth", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    roots_file = workspace / "trusted-roots.local"
    monkeypatch.setenv("TRILOBITE_FILE_ROOTS_FILE", str(roots_file))
    roots_file.write_text(str(repo), encoding="utf-8")

    assert file_ops.read_file(str(target))["text"] == "ground truth"


def test_hot_roots_file_ignores_comments_and_missing_paths(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    roots_file = workspace / "trusted-roots.local"
    monkeypatch.setenv("TRILOBITE_FILE_ROOTS_FILE", str(roots_file))
    roots_file.write_text(
        "# approved roots\n\n" + str(tmp_path / "missing") + "\n",
        encoding="utf-8",
    )

    roots = file_ops.allowed_roots()

    assert workspace.resolve() in roots
    assert (tmp_path / "missing").resolve() in roots


def test_find_files_matches_names(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")

    result = file_ops.find_files("*.py")

    assert [r["relative"] for r in result["results"]] == ["a.py"]


def test_recursive_delete_allows_plain_subdirectory(monkeypatch, tmp_path):
    workspace = tmp_path / "workspace"
    state = tmp_path / "state"
    target = workspace / "scratch"
    target.mkdir(parents=True)
    state.mkdir()
    (target / "ordinary.txt").write_text("delete me", encoding="utf-8")
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.trilobite_paths, "default_home", lambda: state)

    preview = file_ops.delete_path(str(target), recursive=True)
    result = file_ops.delete_path(
        str(target),
        recursive=True,
        dry_run=False,
        confirm=preview["required_confirm"],
    )

    assert result["deleted"] is True
    assert not target.exists()

