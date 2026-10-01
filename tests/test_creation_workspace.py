from __future__ import annotations

import pytest

from sonder_runtime.adapters.creation_workspace import (
    CreationWorkspaceError,
    resolve_writing_workspace,
    writing_project,
)


@pytest.mark.parametrize("project", [None, "", "default", "none", "unknown", "unresolved"])
def test_default_project_uses_run_scoped_creation_workspace(tmp_path, project):
    state_home = tmp_path / "state"

    result = resolve_writing_workspace(project, "auto-56ecb1db616", state_home=state_home)

    assert result == (state_home / "creations" / "auto-56ecb1db616").resolve()
    assert result.is_dir()


def test_explicit_project_is_preserved(tmp_path):
    project = tmp_path / "selected-project"
    project.mkdir()

    result = resolve_writing_workspace(str(project), "run-1", state_home=tmp_path / "state")

    assert result == project
    # Explicit project selection keeps its existing lifecycle.  The resolver
    # must not replace it with a run-scoped directory or create it on behalf
    # of the caller.
    assert result.is_dir()


def test_unresolved_bare_project_uses_run_scoped_creation_workspace(tmp_path):
    state_home = tmp_path / "state"

    result = resolve_writing_workspace("unresolved-project", "run-2", state_home=state_home)

    assert result == (state_home / "creations" / "run-2").resolve()
    assert result.is_dir()


def test_writing_project_preserves_explicit_selector_spelling(tmp_path):
    assert writing_project("./selected-project/", "run-2", state_home=tmp_path / "state") == (
        "./selected-project/"
    )


@pytest.mark.parametrize("run_id", ["", ".", "..", "../escape", "nested/run", "nested\\run", "C:\\escape", "bad:id"])
def test_run_id_cannot_escape_creation_root(tmp_path, run_id):
    with pytest.raises(CreationWorkspaceError):
        resolve_writing_workspace("default", run_id, state_home=tmp_path / "state")


def test_existing_symlinked_creation_root_is_rejected(tmp_path):
    state_home = tmp_path / "state"
    state_home.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    creations = state_home / "creations"
    try:
        creations.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("directory symlinks are unavailable on this host")

    with pytest.raises(CreationWorkspaceError):
        resolve_writing_workspace("default", "run-1", state_home=state_home)


def test_state_home_inside_source_checkout_fails_closed(tmp_path):
    checkout = tmp_path / "sonder"
    (checkout / ".git").mkdir(parents=True)

    with pytest.raises(CreationWorkspaceError):
        resolve_writing_workspace(
            "default",
            "run-1",
            state_home=checkout / "runtime-state",
            source_root=checkout,
        )


@pytest.mark.parametrize("location", ["home", "creations", "run"])
def test_default_workspace_cannot_land_in_nested_or_exact_sonder_checkout(tmp_path, location):
    state = tmp_path / "state"
    target = state if location == "home" else state / "creations"
    if location == "run":
        target /= "run-1"
    (target / ".git").mkdir(parents=True)
    (target / "sonder_runtime").mkdir()
    with pytest.raises(CreationWorkspaceError, match="Sonder source checkout"):
        resolve_writing_workspace("default", "run-1", state_home=state)


def test_existing_directory_named_unknown_is_explicit(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "unknown").mkdir()
    assert writing_project("unknown", "run-1", state_home=tmp_path / "state") == "unknown"
