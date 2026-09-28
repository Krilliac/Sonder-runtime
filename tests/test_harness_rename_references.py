"""find_references / rename_symbol: dot-directory ancestors and atomic rename.

Both helpers skipped any file whose ABSOLUTE path had a dot-prefixed part, so
a project under ``~/.claude/worktrees/...`` (or any ``.``-directory ancestor)
matched nothing.  Only parts below the root are now considered.

``rename_symbol`` also rewrote files one at a time in place, so a failure
midway left a half-renamed tree.  It now stages every rewrite and applies
them with rollback.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import harness_tools


@pytest.fixture(autouse=True)
def _authorize_pytest_tmp_roots(tmp_path_factory, monkeypatch):
    # Same authorization as tests/test_harness_misc.py (the guard itself is
    # covered by tests/test_harness_root_confinement.py).
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path_factory.getbasetemp()))


def _project(tmp_path: Path, ancestor: str = ".hidden-ancestor") -> Path:
    root = tmp_path / ancestor / "project"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "a.py").write_text("def old_name():\n    return 1\n", encoding="utf-8")
    (root / "pkg" / "b.py").write_text("from a import old_name\nold_name()\n", encoding="utf-8")
    (root / ".venv").mkdir()
    (root / ".venv" / "c.py").write_text("old_name\n", encoding="utf-8")
    return root


def test_find_references_under_a_dot_directory_ancestor(tmp_path):
    root = _project(tmp_path)
    result = harness_tools.extract_references(root=str(root), symbol="old_name")
    assert result["ok"]
    files = {Path(ref["file"]).as_posix() for ref in result["references"]}
    # Dot-directories BELOW the root are still skipped.
    assert files == {"pkg/a.py", "pkg/b.py"}


def test_rename_symbol_under_a_dot_directory_ancestor(tmp_path):
    root = _project(tmp_path)
    result = harness_tools.rename_symbol(
        root=str(root), old_name="old_name", new_name="new_name", dry_run=False,
    )
    assert result["ok"] and result["files_changed"] == 2
    assert "new_name" in (root / "pkg" / "a.py").read_text(encoding="utf-8")
    assert "old_name" in (root / ".venv" / "c.py").read_text(encoding="utf-8")


def test_rename_symbol_failure_midway_rolls_every_file_back(tmp_path, monkeypatch):
    root = _project(tmp_path, "plain")
    before = {
        path: path.read_bytes() for path in (root / "pkg").glob("*.py")
    }
    real_replace = harness_tools.os.replace
    calls = {"n": 0}

    def failing_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("disk full")
        return real_replace(src, dst)

    monkeypatch.setattr(harness_tools.os, "replace", failing_replace)
    result = harness_tools.rename_symbol(
        root=str(root), old_name="old_name", new_name="new_name", dry_run=False,
    )
    monkeypatch.setattr(harness_tools.os, "replace", real_replace)
    assert result["ok"] is False
    assert "rolled back" in result["error"]
    assert {path: path.read_bytes() for path in before} == before
    # No staging files are left beside the sources.
    assert sorted(p.name for p in (root / "pkg").iterdir()) == ["a.py", "b.py"]


def test_rename_symbol_preserves_line_endings(tmp_path):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "m.py").write_bytes(b"old_name = 1\r\nprint(old_name)\r\n")
    result = harness_tools.rename_symbol(
        root=str(root), old_name="old_name", new_name="new_name", dry_run=False,
    )
    assert result["ok"]
    assert (root / "m.py").read_bytes() == b"new_name = 1\r\nprint(new_name)\r\n"
