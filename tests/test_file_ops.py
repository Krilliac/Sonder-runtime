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


def test_find_files_matches_names(monkeypatch, tmp_path):
    monkeypatch.setattr(file_ops, "workspace_root", lambda: tmp_path)
    (tmp_path / "a.py").write_text("print(1)", encoding="utf-8")
    (tmp_path / "b.txt").write_text("x", encoding="utf-8")

    result = file_ops.find_files("*.py")

    assert [r["relative"] for r in result["results"]] == ["a.py"]

