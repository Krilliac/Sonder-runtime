"""``workspace_root()`` names Sonder's checkout, not the adapter package.

The strangler migration moved ``file_ops.py`` from the repository root to
``sonder_runtime/adapters/filesystem`` and the "directory of this file"
expression moved with it, so an unscoped relative path resolved into the
adapter package (measured 2026-09-03: an agent's ``ledger/core.py`` became
``sonder_runtime/adapters/filesystem/ledger/core.py``).
"""
from __future__ import annotations

from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops


def test_workspace_root_is_the_directory_containing_the_package():
    root = file_ops.workspace_root()

    assert (root / "sonder_runtime" / "__init__.py").is_file()
    assert (root / "server.py").is_file()
    assert root.name != "filesystem"


def test_an_unscoped_relative_path_resolves_under_the_checkout():
    resolved = file_ops.resolve_path("pytest.ini")

    assert resolved == file_ops.workspace_root() / "pytest.ini"
    assert resolved.is_file()


def test_inside_allowed_roots_reads_the_configured_roots(monkeypatch, tmp_path):
    inside = tmp_path / "proj"
    inside.mkdir()
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))

    assert file_ops.inside_allowed_roots(inside) is True
    assert file_ops.inside_allowed_roots(file_ops.workspace_root()) is True
    assert file_ops.inside_allowed_roots(Path("/")) is False
