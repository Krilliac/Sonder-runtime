"""The Windows name rule on its own, without the overlay-prefix rule behind it.

On Windows a name ending in ``.`` or a space, and ``name::$DATA``, are the same
file as ``name``; ``Path.resolve`` does not always trim them, so a name check
can miss them.  ``path_aliases.win32_alias_component`` finds such a component
in the caller's raw text and every file tool refuses it before any other
check.  The overlay tests in ``test_policy_file_names`` cannot show that the
rule works: the overlay-prefix rule refuses those spellings too.  These tests
aim at control-plane files that only the exact-name inventory protects (the
state databases, their ``-journal`` / ``-wal`` sidecars and ``file_roots.local``)
while they do not exist yet, and at the read and restricted-lane checks, so
each would let the alias through if the rule returned ``""``.
"""
from __future__ import annotations

import pytest

import server
from sonder_runtime.adapters import restricted_tool_lane as lane
from sonder_runtime.adapters.filesystem import file_ops
from tests.support.policy_checkout import windows_only

pytestmark = windows_only
pytest_plugins = ("tests.support.policy_checkout",)  # the policy_files fixture

SUFFIXES = pytest.mark.parametrize("suffix", [".", " ", "::$DATA"], ids=["dot", "space", "ads"])
NAMES = pytest.mark.parametrize("name", [
    "approvals.db", "agent-effects.db", "approvals.db-journal", "agent-effects.db-wal", "file_roots.local",
])


@pytest.fixture
def control(policy_files, tmp_path, monkeypatch):
    """A folder holding exact control-plane files by override, none of them created yet."""
    folder = tmp_path / "ctl"
    folder.mkdir()
    monkeypatch.setenv("SONDER_APPROVALS_DB", str(folder / "approvals.db"))
    monkeypatch.setenv("SONDER_AGENT_EFFECTS_DB", str(folder / "agent-effects.db"))
    monkeypatch.setenv("SONDER_FILE_ROOTS_FILE", str(folder / "file_roots.local"))
    for name in ("approvals.db", "agent-effects.db-wal", "file_roots.local"):
        assert file_ops._is_protected_mutation_path(folder / name), name
    return folder


def _nothing_created(folder):
    assert sorted(entry.name for entry in folder.iterdir()) == []


@SUFFIXES
@NAMES
@pytest.mark.parametrize("developer", [False, True], ids=["no_token", "developer_token"])
def test_write_file_refuses_a_win32_spelling_of_a_missing_control_file(control, name, suffix, developer):
    with pytest.raises(PermissionError):
        file_ops.write_file(str(control / name) + suffix, "x\n", mode="create", developer_authorized=developer)
    _nothing_created(control)


@SUFFIXES
@NAMES
@pytest.mark.parametrize("developer", [False, True], ids=["no_token", "developer_token"])
def test_the_file_write_tool_refuses_a_win32_spelling_of_a_missing_control_file(
    control, monkeypatch, name, suffix, developer,
):
    monkeypatch.setattr(server, "_maybe_live_reload", lambda *a, **k: None)
    monkeypatch.setattr(server, "_file_developer_allowed", lambda token="": token == "dev")
    out = server.file_write(str(control / name) + suffix, "x\n", token="dev" if developer else "")
    assert out.startswith("ERROR"), out
    _nothing_created(control)


def test_an_ordinary_name_in_the_control_folder_is_written(control):
    """Control: the folder is not what is refused, and the token still writes an ordinary file there."""
    file_ops.write_file(str(control / "notes.txt"), "x\n", mode="create", developer_authorized=True)
    file_ops.write_file(str(control / "more.txt"), "x\n", mode="create")
    assert (control / "notes.txt").read_text(encoding="utf-8") == "x\n"


# For an existing file ``Path.resolve`` already returns the trimmed name for ``x.`` and ``x::$DATA``, so
# those two read cases are preservation tests; a named stream (``x:hidden``) keeps its spelling through
# resolve, and only the rule refuses it.
@pytest.mark.parametrize("suffix", [".", "::$DATA", ":hidden"], ids=["dot", "ads", "named_stream"])
@pytest.mark.parametrize("name", ["server.pem", "approvals.db"])
def test_a_repository_read_refuses_a_win32_spelling(control, name, suffix):
    target = control / name
    target.write_text("SECRET\n", encoding="utf-8")
    if suffix == ":hidden":
        try:
            with open(str(target) + suffix, "w", encoding="utf-8") as stream:
                stream.write("SECRET IN A STREAM\n")
        except OSError:
            pytest.skip("this volume has no alternate data streams")
    with pytest.raises(PermissionError):
        file_ops.resolve_repository_read_path(str(target) + suffix)
    # Control: an ordinary file in the same folder resolves.
    plain = control / "readme.txt"
    plain.write_text("ok\n", encoding="utf-8")
    assert file_ops.resolve_repository_read_path(str(plain)) == plain.resolve()


# ``.env:hidden`` (a named stream) keeps its spelling through realpath and matches no glob: without the
# rule it is read.  For the other spellings realpath returns ``.env`` and the glob would still refuse;
# the rule refuses them first, by name.
@pytest.mark.parametrize("value", [".env:hidden", ".env::$DATA", "config/credentials.json::$DATA", ".env. "])
def test_a_restricted_run_refuses_an_alias_spelling(tmp_path, value):
    project = tmp_path / "proj"
    (project / "config").mkdir(parents=True)
    (project / ".env").write_text("needle=TOPSECRET\n", encoding="utf-8")
    (project / "config" / "credentials.json").write_text("{}\n", encoding="utf-8")
    if value == ".env:hidden":
        try:
            with open(str(project / value), "w", encoding="utf-8") as stream:
                stream.write("needle=TOPSECRET\n")
        except OSError:
            pytest.skip("this volume has no alternate data streams")
    run = lane.RestrictedRun(
        lane="agent", model="m", tools=frozenset({"file_read_range"}), profiles=("security-advisers",),
        secret_globs=(".env", "*credential*"), caller_project="", target=("x", False, None, "model:m"),
    )
    run.project_root = str(project)
    globs = (".env", "*credential*")
    refused = run.path_refusal("file_read_range", {"path": value}, globs)
    assert "Windows alias" in refused, refused
    # Control: the plain spelling is refused by the secret glob, an ordinary file not at all.
    assert "secret path glob" in run.path_refusal("file_read_range", {"path": ".env"}, globs)
    assert run.path_refusal("file_read_range", {"path": "config/app.py"}, globs) == ""
