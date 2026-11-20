"""The server git guard refuses driver names it cannot override safely.

``driver_overrides`` builds ``-c <section>.<name>.<key>=<value>``. git splits
a ``-c`` argument at the first '=', so a driver named ``a=b`` turns
``-c filter.a=b.clean=cat`` into ``filter.a`` = ``b.clean=cat`` and the real
``filter "a=b"`` clean program stays live. Names outside ``[A-Za-z0-9_.-]``
must raise :class:`GitProgramConfigError`, so callers refuse to run git.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

import git_tools
from sonder_runtime.adapters import git_program_guard
from sonder_runtime.adapters.filesystem import file_ops


@pytest.mark.parametrize(
    "key",
    [
        "filter.a=b.clean",
        "filter.a=b.process",
        "merge.m=x.driver",
        "diff.d=e.textconv",
        "filter.a b.clean",
        "filter.a\tb.clean",
        "filter.a\nb.clean",
        "filter.a\"b.clean",
        "filter.a/b.clean",
        "filter.\u00e9.clean",
        "filter.a\ufffdb.clean",
        "filter.\u212a.clean",
    ],
)
def test_unsafe_driver_name_is_refused(key: str):
    with pytest.raises(git_program_guard.GitProgramConfigError):
        git_program_guard.driver_overrides([key])


@pytest.mark.parametrize(
    "key",
    ["filter.lfs.clean", "filter.my-filter_2.smudge", "filter.a.b.process", "merge.Ours.driver"],
)
def test_safe_driver_name_is_overridden(key: str):
    overrides = git_program_guard.driver_overrides([key])
    section, _, rest = key.partition(".")
    name = rest.rsplit(".", 1)[0]
    assert any(arg.startswith(f"{section.lower()}.{name}.") for arg in overrides)
    for arg in overrides[1::2]:
        assert arg.split("=", 1)[0].count(".") >= 2


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        stdin=subprocess.DEVNULL, capture_output=True, text=True, check=False,
    )
    if result.returncode:
        raise AssertionError(result.stderr or result.stdout)
    return result.stdout


def _marker_program(tmp_path: Path, marker: Path) -> str:
    """A Python clean filter (works on Windows and POSIX) that records it ran."""
    script = tmp_path / "clean-marker.py"
    script.write_text(
        "import sys\n"
        f"with open({str(marker)!r}, 'a', encoding='utf-8') as handle:\n"
        "    handle.write('ran\\n')\n"
        "sys.stdout.buffer.write(sys.stdin.buffer.read())\n",
        encoding="utf-8",
    )
    python = str(sys.executable).replace("\\", "/")
    return f'"{python}" "{script.as_posix()}"'


@pytest.fixture
def equals_filter_repo(tmp_path: Path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.name", "Sonder Tests")
    _git(repo, "config", "user.email", "sonder-tests@example.invalid")
    (repo / ".gitattributes").write_text("*.txt filter=a=b\n", encoding="utf-8")
    (repo / "tracked.txt").write_text("same\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    marker = tmp_path / "clean.txt"
    with open(repo / ".git" / "config", "a", encoding="utf-8") as handle:
        handle.write(f'[filter "a=b"]\n\tclean = {_marker_program(tmp_path, marker)}\n')
    tracked = repo / "tracked.txt"
    stat = tracked.stat()
    os.utime(tracked, (stat.st_atime + 120, stat.st_mtime + 120))
    monkeypatch.setattr(file_ops, "workspace_root", lambda: repo)
    return repo, marker


def test_repo_status_refuses_a_filter_named_with_equals(equals_filter_repo):
    repo, marker = equals_filter_repo

    refusal = ""
    try:
        git_tools.repo_status(str(repo), bypass=True)
    except PermissionError as exc:
        refusal = str(exc)

    assert not marker.exists(), "the guarded git ran the clean filter named 'a=b'"
    assert "refusing to run git" in refusal
    subprocess.run(["git", "status"], cwd=repo, capture_output=True, check=False)
    assert marker.exists(), "control: plain git must run the clean filter named 'a=b'"
