"""Tests for V3-realpath-cwd: resolve_cwd must resolve symlinks before the
workspace containment check, mirroring file_ops.resolve_path semantics.

code_runner is explicitly not a security sandbox (see its module docstring);
these tests pin the tidiness/consistency behavior that a symlink living inside
the workspace but pointing outside it cannot silently place the child process
cwd outside the tree.
"""
import os

import pytest

import code_runner


def _symlinks_supported(tmp_path):
    target = tmp_path / "_probe_target"
    target.mkdir()
    link = tmp_path / "_probe_link"
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError, AttributeError):
        return False
    return os.path.islink(str(link))


def test_symlink_escaping_workspace_is_rejected(monkeypatch, tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlinks unavailable on this platform")
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    # A symlink that lives inside the workspace but points outside it.
    escape = root / "escape"
    os.symlink(outside, escape)
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))
    with pytest.raises(ValueError):
        code_runner.resolve_cwd(str(escape))


def test_relative_symlink_escaping_workspace_is_rejected(monkeypatch, tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlinks unavailable on this platform")
    root = tmp_path / "repo"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    escape = root / "escape"
    os.symlink(outside, escape)
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))
    # Passed as a relative path, joined onto the (symlinked) workspace root.
    with pytest.raises(ValueError):
        code_runner.resolve_cwd("escape")


def test_symlink_pointing_inside_workspace_is_allowed(monkeypatch, tmp_path):
    if not _symlinks_supported(tmp_path):
        pytest.skip("symlinks unavailable on this platform")
    root = tmp_path / "repo"
    real = root / "real"
    real.mkdir(parents=True)
    # A symlink inside the workspace that points to another dir inside it.
    link = root / "link"
    os.symlink(real, link)
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))
    resolved = code_runner.resolve_cwd(str(link))
    assert resolved == os.path.realpath(str(real))


def test_normal_absolute_path_still_resolves(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    child = root / "child"
    child.mkdir(parents=True)
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))
    resolved = code_runner.resolve_cwd(str(child))
    assert resolved == os.path.realpath(str(child))


def test_real_subdirectory_is_unaffected(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    nested = root / "a" / "b" / "c"
    nested.mkdir(parents=True)
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))
    # Relative and absolute forms agree and stay inside the workspace.
    assert code_runner.resolve_cwd("a/b/c") == os.path.realpath(str(nested))
    assert code_runner.resolve_cwd(str(nested)) == os.path.realpath(str(nested))


def test_empty_cwd_returns_workspace_root(monkeypatch, tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(code_runner, "workspace_root", lambda: str(root))
    assert code_runner.resolve_cwd() == os.path.realpath(str(root))
    assert code_runner.resolve_cwd("") == os.path.realpath(str(root))
