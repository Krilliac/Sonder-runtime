"""Model file tools must not reach control state that raises Sonder's autonomy.

Each test pins one reviewed hole in the file_ops mutation/read classifier:

* ``permission_mode.json`` in the state home was writable by ``file_write``,
  so a mutation-class call (allowed unattended in ``acceptEdits``) could
  persist ``auto`` and bypass the attended-only mode raise.
* Writes into ``.git`` (hooks, config) were ordinary mutations, and the next
  git command then ran the planted program.
* Only root-level and ``sonder_runtime/`` ``.py`` files were protected, so
  the selfmod isolation/oracle scripts and the nightly runners that the
  runtime imports or executes were writable.
* ``*.env`` files such as ``<home>/secrets.env`` were not classified as
  secrets, so they could be read, copied and archived.
"""
from __future__ import annotations

import pytest

import sonder_runtime.adapters.filesystem.file_ops as file_ops


@pytest.fixture()
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    ws.mkdir()
    home.mkdir()
    monkeypatch.setattr(file_ops, "workspace_root", lambda: ws)
    monkeypatch.setattr(file_ops.runtime_paths, "default_home", lambda: home)
    monkeypatch.delenv("SONDER_FILE_BYPASS", raising=False)
    monkeypatch.delenv("SONDER_FILE_APPROVAL_CODE", raising=False)
    return ws, home


def test_state_home_permission_mode_file_is_protected(workspace):
    _ws, home = workspace
    target = home / "permission_mode.json"
    target.write_text('{"mode": "manual"}', encoding="utf-8")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(str(target), '{"mode": "auto"}', mode="overwrite")
    assert target.read_text(encoding="utf-8") == '{"mode": "manual"}'


def test_permission_mode_atomic_temp_sibling_is_protected(workspace):
    _ws, home = workspace
    sibling = home / "permission_mode.json.tmp-abc123"
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(str(sibling), '{"mode": "auto"}')


def test_permission_mode_file_developer_token_still_writes(workspace):
    _ws, home = workspace
    target = home / "permission_mode.json"
    out = file_ops.write_file(
        str(target), '{"mode": "manual"}', developer_authorized=True,
    )
    assert out["bytes"] > 0


@pytest.mark.parametrize("relative", [
    ".git/hooks/pre-commit",
    ".git/config",
    "nested/project/.git/hooks/post-checkout",
    ".GIT/hooks/pre-commit",
])
def test_writes_into_git_metadata_are_refused(workspace, relative):
    ws, _home = workspace
    (ws / relative).parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(PermissionError):
        file_ops.write_file(relative, "#!/bin/sh\necho planted\n", mode="overwrite")
    assert not (ws / relative).exists()


def test_git_metadata_copy_destination_and_delete_are_refused(workspace):
    ws, _home = workspace
    (ws / ".git" / "hooks").mkdir(parents=True)
    (ws / "payload.sh").write_text("echo planted\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        file_ops.copy_file("payload.sh", ".git/hooks/pre-commit")
    (ws / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        file_ops.delete_path(".git/HEAD")
    assert (ws / ".git" / "HEAD").exists()


def test_git_metadata_write_allowed_for_developer(workspace):
    ws, _home = workspace
    (ws / ".git").mkdir()
    out = file_ops.write_file(
        ".git/description", "x", developer_authorized=True,
    )
    assert out["bytes"] == 1


def test_ordinary_names_containing_git_stay_writable(workspace):
    ws, _home = workspace
    out = file_ops.write_file(".github/workflows/ci.yml", "on: push\n")
    assert out["bytes"] > 0
    out = file_ops.write_file("docs/.gitignore", "*.tmp\n")
    assert out["bytes"] > 0


@pytest.mark.parametrize("relative", [
    "scripts/selfmod_linux_isolation.py",
    "scripts/selfmod_oracle.py",
    "scripts/nightly_self_improve.py",
    "scripts/nightly_selfmod.py",
    "scripts/run-nightly.ps1",
    "sonder-runtime.cmd",
    "sonder-runtime.sh",
    "migrations/memory/0001_baseline.py",
    "tests/conftest.py",
])
def test_first_party_executable_code_is_protected(workspace, relative):
    ws, _home = workspace
    target = ws / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("original = True\n", encoding="utf-8")
    with pytest.raises(PermissionError, match="mutate protected Sonder control-plane"):
        file_ops.write_file(relative, "import os\nos.system('payload')\n", mode="overwrite")
    assert target.read_text(encoding="utf-8") == "original = True\n"


def test_nested_user_project_code_stays_writable(workspace):
    ws, _home = workspace
    (ws / "project" / "scripts").mkdir(parents=True)
    out = file_ops.write_file("project/scripts/build.py", "value = 1\n")
    assert out["bytes"] > 0
    out = file_ops.write_file("project/run.sh", "echo hi\n")
    assert out["bytes"] > 0
    out = file_ops.write_file("notes/readme.md", "# notes\n")
    assert out["bytes"] > 0


@pytest.mark.parametrize("name", ["secrets.env", "prod.env", "SONDER.ENV", ".envrc"])
def test_dotenv_style_files_are_secret(workspace, name):
    _ws, home = workspace
    target = home / name
    target.write_text("SONDER_API_KEY=sk-test-value\n", encoding="utf-8")
    assert file_ops._is_secret_path(target)
    with pytest.raises(PermissionError):
        file_ops.read_file(str(target))


def test_secrets_env_cannot_be_copied_into_a_project(workspace):
    ws, home = workspace
    (home / "secrets.env").write_text("SONDER_API_KEY=sk-test-value\n", encoding="utf-8")
    with pytest.raises(PermissionError):
        file_ops.copy_file(str(home / "secrets.env"), "notes.txt")
    assert not (ws / "notes.txt").exists()


def test_environment_named_source_files_are_not_secrets(workspace):
    ws, _home = workspace
    (ws / "environment.py").write_text("x = 1\n", encoding="utf-8")
    (ws / "project").mkdir()
    (ws / "project" / "env.md").write_bytes(b"docs\n")
    assert not file_ops._is_secret_path(ws / "environment.py")
    assert file_ops.read_file("project/env.md")["text"] == "docs\n"
