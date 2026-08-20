"""Regression coverage for the packaged filesystem path seam."""

from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops


def test_roots_file_path_uses_packaged_default_home(monkeypatch, tmp_path):
    home = tmp_path / "sonder-home"
    monkeypatch.delenv("SONDER_FILE_ROOTS_FILE", raising=False)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: home)

    assert file_ops.roots_file_path() == home / file_ops.DEFAULT_ROOTS_FILE


def test_allowed_roots_preserves_workspace_and_default_home_containment(
    monkeypatch, tmp_path
):
    workspace = tmp_path / "workspace"
    home = tmp_path / "sonder-home"
    monkeypatch.setattr(file_ops, "workspace_root", lambda: workspace)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_ROOTS", raising=False)
    monkeypatch.delenv("SONDER_FILE_ROOTS_FILE", raising=False)

    roots = file_ops.allowed_roots()

    assert workspace.resolve() in roots
    assert home.resolve() in roots
    assert file_ops._is_inside(home / "permissions.json", home)
    assert not file_ops._is_inside(tmp_path / "outside.txt", home)


def test_default_home_path_is_resolved_before_containment_check(monkeypatch, tmp_path):
    home = tmp_path / "sonder-home"
    monkeypatch.setattr(
        file_ops.runtime_paths,
        "default_home",
        lambda: home / "nested" / "..",
    )

    assert file_ops._resolve_best_effort(Path(file_ops.runtime_paths.default_home())) == home
