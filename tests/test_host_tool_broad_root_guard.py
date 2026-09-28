"""A home-wide file root must not make a planted binary a trusted host tool.

``_project_roots`` skips roots that are the filesystem root, the home
directory or an ancestor of home (classifying them as project roots would
mark every installed tool as planted). Before this fix that skip left the
whole broad root undefended: a model writing ``~/bin/git.exe`` through a
home-wide root got it accepted by ``executable_allowed``. Now an executable
under such a root is trusted only when this user cannot modify it.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

import pytest

from sonder_runtime.adapters.host_tools import guards


def _roots(monkeypatch, home: Path, roots):
    monkeypatch.setattr(guards, "_home", lambda: str(home))
    monkeypatch.setattr(
        guards.file_ops, "allowed_roots", lambda extra_roots="": [Path(r) for r in roots],
    )


def _plant(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / ("git.exe" if os.name == "nt" else "git")
    shutil.copyfile(sys.executable, target)
    target.chmod(0o755)
    return target


def test_planted_binary_under_home_wide_root_is_not_trusted(tmp_path, monkeypatch):
    home = tmp_path / "home"
    planted = _plant(home / "bin")
    _roots(monkeypatch, home, [home])
    assert guards.project_local(str(planted))
    assert not guards.executable_allowed(str(planted))
    with pytest.raises(PermissionError):
        guards.require_host_executable(str(planted))


def test_planted_binary_under_ancestor_of_home_root_is_not_trusted(tmp_path, monkeypatch):
    home = tmp_path / "users" / "me"
    planted = _plant(tmp_path / "users" / "shared" / "tools")
    _roots(monkeypatch, home, [tmp_path / "users"])
    assert not guards.executable_allowed(str(planted))


def test_writable_path_directory_under_broad_root_is_project_local(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    _roots(monkeypatch, home, [home])
    assert guards.project_local(str(home / "bin"))


def test_unmodifiable_system_binary_under_filesystem_root_stays_trusted(tmp_path, monkeypatch):
    if os.name == "nt":
        system = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "where.exe"
    else:
        system = Path("/bin/sh")
        if os.geteuid() == 0:
            pytest.skip("root can modify every system binary")
    if not system.is_file():
        pytest.skip("no system binary to probe")
    _roots(monkeypatch, tmp_path / "home", [Path(system.anchor)])
    assert not guards.project_local(str(system))
    assert guards.executable_allowed(str(system))


def test_binary_outside_every_root_is_unchanged(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    planted = _plant(tmp_path / "elsewhere")
    _roots(monkeypatch, home, [tmp_path / "project"])
    assert guards.executable_allowed(str(planted))
