"""The client companion must not turn an acceptEdits write into code execution.

acceptEdits lets file changes through without a prompt and keeps the execute
prompt. The git tools are classed read-only, so any repository configuration
that makes git launch a program (``core.fsmonitor``, filter/textconv drivers,
``diff.external``, hooks) would let a write reach execution with no execute
prompt. These tests pin both halves of the fix: writes to git-executable
configuration are refused, and every git subprocess runs with that
configuration neutralized, including for a repository the tools did not
write (an existing checkout, or an embedded bare repository whose files carry
no ``.git`` component at all).

Each hardening test ends with a positive control: the same payload run
through plain ``git`` must create the marker, so a missing marker means the
hardening held and not that the payload was broken.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from sonder_runtime.interfaces.client_agent import (
    ClientAgentPolicy,
    execute_client_tool,
)

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(ws: Path) -> None:
    ws.mkdir(parents=True, exist_ok=True)
    _git(ws, "init", "-q")
    _git(ws, "config", "user.email", "test@example.com")
    _git(ws, "config", "user.name", "Test")
    _git(ws, "config", "commit.gpgSign", "false")


def _payload(tmp_path: Path) -> tuple[str, Path]:
    """A command git would run, and the marker it creates when it does."""
    marker = tmp_path / "marker.txt"
    script = tmp_path / "payload.sh"
    script.write_text(f"echo pwned > {marker.as_posix()}\n", encoding="utf-8")
    if " " in script.as_posix():
        pytest.skip("payload path contains a space; git config value would need quoting")
    return f"sh {script.as_posix()}", marker


def _append_config(ws: Path, text: str) -> None:
    config = ws / ".git" / "config"
    config.write_text(config.read_text(encoding="utf-8") + text, encoding="utf-8")


@pytest.mark.parametrize(
    "rel_path",
    [
        ".git/config",
        ".git/hooks/pre-commit",
        ".git/info/attributes",
        ".git/new-file",
        ".GIT/config",
        "sub/.git/config",
        ".gitattributes",
        "sub/.gitattributes",
        ".GitAttributes",
        ".gitmodules",
    ],
)
def test_accept_edits_refuses_write_to_git_executable_config(tmp_path: Path, rel_path: str):
    ws = tmp_path / "repo"
    _init_repo(ws)
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")
    before = (ws / ".git" / "config").read_text(encoding="utf-8")

    res = execute_client_tool(
        "file_write", {"path": rel_path, "content": "[core]\n\tfsmonitor = sh h.sh\n"}, policy=policy,
    )

    assert res.ok is False
    assert res.denied is True
    assert "git" in res.error
    assert (ws / ".git" / "config").read_text(encoding="utf-8") == before
    leaf = Path(rel_path).name.casefold()
    if leaf in {".gitattributes", ".gitmodules"}:
        assert not (ws / rel_path).exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows trailing-dot and stream spellings")
@pytest.mark.parametrize("rel_path", [".git./config", ".git /config", ".git::$INDEX_ALLOCATION/config"])
def test_windows_alias_spellings_of_git_dir_are_refused(tmp_path: Path, rel_path: str):
    ws = tmp_path / "repo"
    _init_repo(ws)
    before = (ws / ".git" / "config").read_text(encoding="utf-8")
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    res = execute_client_tool("file_write", {"path": rel_path, "content": "x"}, policy=policy)

    assert res.ok is False and res.denied is True
    assert (ws / ".git" / "config").read_text(encoding="utf-8") == before


@pytest.mark.parametrize("mode", ["acceptEdits", "auto", "bypass"])
def test_edit_delete_and_mkdir_under_git_dir_are_refused(tmp_path: Path, mode: str):
    ws = tmp_path / "repo"
    _init_repo(ws)
    kwargs = {"confirm_bypass": True} if mode == "bypass" else {}
    policy = ClientAgentPolicy(workspace_root=ws, mode="manual", can_bypass=(mode == "bypass"))
    policy.set_mode(mode, **kwargs)
    config_before = (ws / ".git" / "config").read_text(encoding="utf-8")

    edit = execute_client_tool(
        "file_edit", {"path": ".git/config", "old": "[core]", "new": "[core]\n\tfsmonitor = x"}, policy=policy,
    )
    delete = execute_client_tool("file_delete", {"path": ".git/HEAD"}, policy=policy)
    mkdir = execute_client_tool("directory_create", {"path": ".git/hooks/extra"}, policy=policy)

    for res in (edit, delete, mkdir):
        assert res.ok is False and res.denied is True
    assert (ws / ".git" / "config").read_text(encoding="utf-8") == config_before
    assert (ws / ".git" / "HEAD").exists()
    assert not (ws / ".git" / "hooks" / "extra").exists()


def test_ordinary_dotfiles_next_to_git_dir_are_still_writable(tmp_path: Path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")
    for rel in (".gitignore", ".github/workflows/ci.yml", "docs/.git-notes.md", "gitconfig.txt"):
        res = execute_client_tool("file_write", {"path": rel, "content": "ok\n"}, policy=policy)
        assert res.ok is True, (rel, res.error)


def test_accept_edits_fsmonitor_chain_from_the_finding_does_not_execute(tmp_path: Path):
    """The reviewer's exact chain: write h.sh, write .git/config, call git_status."""
    ws = tmp_path / "repo"
    _init_repo(ws)
    marker = ws / "marker.txt"
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")
    original = (ws / ".git" / "config").read_text(encoding="utf-8")

    script = execute_client_tool(
        "file_write", {"path": "h.sh", "content": "echo pwned > marker.txt\n"}, policy=policy,
    )
    config = execute_client_tool(
        "file_write",
        {"path": ".git/config", "content": original.replace("[core]", "[core]\n\tfsmonitor = sh h.sh", 1)},
        policy=policy,
    )
    status = execute_client_tool("git_status", {}, policy=policy)
    run_program = execute_client_tool("run_program", {"command": "git status"}, policy=policy)

    assert script.ok is True
    assert config.ok is False and config.denied is True
    assert status.ok is True and status.risk == "read_only"
    assert run_program.needs_approval is True
    assert not marker.exists()


def test_git_status_neutralizes_existing_fsmonitor(tmp_path: Path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    command, marker = _payload(tmp_path)
    _append_config(ws, f"[core]\n\tfsmonitor = {command}\n")
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    res = execute_client_tool("git_status", {}, policy=policy)

    assert res.ok is True, res.error
    assert not marker.exists()
    subprocess.run(["git", "status"], cwd=ws, capture_output=True, check=False)
    assert marker.exists(), "control: plain git must run the fsmonitor payload"


def test_git_status_neutralizes_filter_clean_driver(tmp_path: Path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    (ws / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")
    (ws / "a.txt").write_text("same\n", encoding="utf-8")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "init")
    command, marker = _payload(tmp_path)
    _append_config(ws, f'[filter "evil"]\n\tclean = {command}\n')
    # Same size, new mtime: git has to re-hash the file through the clean filter.
    stat = (ws / "a.txt").stat()
    os.utime(ws / "a.txt", (stat.st_atime + 120, stat.st_mtime + 120))
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    res = execute_client_tool("git_status", {}, policy=policy)

    assert res.ok is True, res.error
    assert not marker.exists()
    subprocess.run(["git", "status"], cwd=ws, capture_output=True, check=False)
    assert marker.exists(), "control: plain git must run the clean filter"


@pytest.mark.parametrize("kind", ["external", "textconv"])
def test_git_diff_and_log_neutralize_diff_programs(tmp_path: Path, kind: str):
    ws = tmp_path / "repo"
    _init_repo(ws)
    (ws / "a.txt").write_text("one\n", encoding="utf-8")
    if kind == "textconv":
        (ws / ".gitattributes").write_text("*.txt diff=evil\n", encoding="utf-8")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "init")
    (ws / "a.txt").write_text("two two\n", encoding="utf-8")
    command, marker = _payload(tmp_path)
    if kind == "external":
        _append_config(ws, f"[diff]\n\texternal = {command}\n")
    else:
        _append_config(ws, f'[diff "evil"]\n\ttextconv = {command}\n')
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    diff = execute_client_tool("git_diff", {}, policy=policy)
    log = execute_client_tool("git_log", {"limit": 3}, policy=policy)

    assert diff.ok is True, diff.error
    assert log.ok is True, log.error
    assert not marker.exists()
    subprocess.run(["git", "diff"], cwd=ws, capture_output=True, check=False)
    assert marker.exists(), f"control: plain git diff must run the {kind} program"


def test_embedded_bare_repository_written_without_git_component_cannot_execute(tmp_path: Path):
    """A bare repository needs no '.git' path component, so the write guard alone is not enough."""
    ws = tmp_path / "ws"
    ws.mkdir()
    command, marker = _payload(tmp_path)
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")
    writes = [
        execute_client_tool("directory_create", {"path": "evil/objects"}, policy=policy),
        execute_client_tool("directory_create", {"path": "evil/refs"}, policy=policy),
        execute_client_tool("file_write", {"path": "evil/HEAD", "content": "ref: refs/heads/main\n"}, policy=policy),
        execute_client_tool(
            "file_write",
            {
                "path": "evil/config",
                "content": (
                    "[core]\n\trepositoryformatversion = 0\n\tbare = false\n"
                    f"\tworktree = ..\n\tfsmonitor = {command}\n"
                ),
            },
            policy=policy,
        ),
    ]
    assert all(w.ok for w in writes), [w.error for w in writes]

    res = execute_client_tool("git_status", {"cwd": "evil"}, policy=policy)

    assert not marker.exists()
    assert res.risk == "read_only"
    subprocess.run(["git", "status"], cwd=ws / "evil", capture_output=True, check=False)
    assert marker.exists(), "control: plain git must treat evil/ as a repository and run fsmonitor"


def test_git_executable_inside_workspace_is_never_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    ws = tmp_path / "repo"
    _init_repo(ws)
    planted = ws / ("git.exe" if os.name == "nt" else "git")
    planted.write_text("not a real git\n", encoding="utf-8")
    from sonder_runtime.interfaces.client_agent import executor as executor_mod

    monkeypatch.setattr(executor_mod.shutil, "which", lambda *a, **k: str(planted))
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    res = execute_client_tool("git_status", {}, policy=policy)

    assert res.ok is False
    assert "inside the workspace" in res.error


def test_git_tools_still_work_on_a_normal_repository(tmp_path: Path):
    ws = tmp_path / "repo"
    _init_repo(ws)
    (ws / "a.txt").write_text("one\n", encoding="utf-8")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "first commit")
    (ws / "a.txt").write_text("two\n", encoding="utf-8")
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    status = execute_client_tool("git_status", {}, policy=policy)
    diff = execute_client_tool("git_diff", {"path": "a.txt"}, policy=policy)
    log = execute_client_tool("git_log", {"limit": 5}, policy=policy)
    branch = execute_client_tool("git_branch", {}, policy=policy)

    assert status.ok and "a.txt" in status.output
    assert diff.ok and "+two" in diff.output
    assert log.ok and "first commit" in log.output
    assert branch.ok and branch.output.strip()


def test_driver_name_containing_equals_fails_closed(tmp_path: Path):
    """``-c filter.a=b.process=`` sets ``filter.a``: an '=' in the name must refuse, not mis-override."""
    ws = tmp_path / "repo"
    _init_repo(ws)
    (ws / ".gitattributes").write_text("*.txt filter=a=b\n", encoding="utf-8")
    (ws / "a.txt").write_text("same\n", encoding="utf-8")
    _git(ws, "add", ".")
    _git(ws, "commit", "-q", "-m", "init")
    command, marker = _payload(tmp_path)
    _append_config(ws, f'[filter "a=b"]\n\tclean = {command}\n')
    stat = (ws / "a.txt").stat()
    os.utime(ws / "a.txt", (stat.st_atime + 120, stat.st_mtime + 120))
    policy = ClientAgentPolicy(workspace_root=ws, mode="acceptEdits")

    res = execute_client_tool("git_status", {}, policy=policy)

    assert not marker.exists()
    assert res.ok is False
    assert "refusing to run git" in res.error
    subprocess.run(["git", "status"], cwd=ws, capture_output=True, check=False)
    assert marker.exists(), "control: plain git must run the clean filter named 'a=b'"
