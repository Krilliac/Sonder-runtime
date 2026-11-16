"""Other names for the model tool policy files, and the writes that reach them without a file tool.

The overlay (``<state home>/model_tool_policy.json``) does not exist until an
operator creates it, and a path that does not exist cannot be compared by
identity.  So the guard also works on the spelling:

* Windows resolves a trailing dot or space, and ``::$DATA``, to the same file
  (``model_tool_policy.json.`` is ``model_tool_policy.json``); the caller's raw
  text is refused before any other check, for reads too (a secret read through
  ``server.pem::$DATA`` is the same file as ``server.pem``);
* a name in the overlay's directory that starts like the overlay
  (``model_tool_policy*``, or the stem of a renamed overlay) is refused, with
  or without a developer token;
* a hard link to a protected file is another name for it: a formatter or lint
  fix run over a folder holding one must be refused, but unrelated hard links
  (pnpm stores, uv environments) must not stop formatting;
* a developer-token recursive delete never follows a junction or symlink out
  of the folder it was asked to delete;
* ``archive_extract`` to a new directory cannot create a missing overlay.

Controls show the same call working on an ordinary name.
"""
from __future__ import annotations

import os
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import harness_tools
import server
from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters import protected_write_scope as scope
from sonder_runtime.adapters.filesystem import file_ops
from tests.support.policy_checkout import windows_only

pytest_plugins = ("tests.support.policy_checkout",)  # the policy_files fixture

BODY = '{"version": 1, "model_profiles": [{"names": ["hostile-model"], "tools": ["file_read_range"]}]}\n'


@pytest.fixture
def no_child(monkeypatch):
    calls = []

    def run(cmd, *, cwd, timeout=None, **kwargs):
        calls.append((list(cmd), Path(cwd)))
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(harness_tools, "_run", run)
    return calls


def _link_directory(link, target):
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                           check=True, capture_output=True)
        else:
            link.symlink_to(target, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("cannot create a directory link here")


# ---- Windows spellings of a missing overlay -------------------------------------------

@windows_only
@pytest.mark.parametrize("developer", [False, True], ids=["no_token", "developer_token"])
@pytest.mark.parametrize("suffix", [".", " ", ". ", "::$DATA"], ids=["dot", "space", "dot_space", "ads"])
def test_a_win32_spelling_cannot_create_a_missing_overlay(policy_files, suffix, developer):
    overlay = policy_files.overlay
    overlay.unlink()
    spelled = str(overlay) + suffix
    with pytest.raises(PermissionError):
        file_ops.write_file(spelled, BODY, mode="create", developer_authorized=developer)
    assert not overlay.exists()
    assert mtp.load().overlay_present is False
    # Control: an ordinary new file in the same folder is created (the folder is not what is refused).
    plain = overlay.with_name("notes.txt")
    file_ops.write_file(str(plain), "x\n", mode="create", developer_authorized=True)
    assert plain.read_text(encoding="utf-8") == "x\n"


@windows_only
def test_the_mcp_file_write_refuses_a_trailing_dot_overlay(policy_files):
    overlay = policy_files.overlay
    overlay.unlink()
    out = server.file_write(str(overlay) + ".", BODY)
    assert out.startswith("ERROR"), out
    assert not overlay.exists()


@windows_only
def test_the_win32_name_rule_leaves_dot_components_alone(policy_files):
    """``..`` is a path component, not a trailing-dot name: a path that uses one still writes an ordinary file."""
    (policy_files.project / "sub").mkdir()
    target = policy_files.project / "sub" / ".." / "okay.txt"
    file_ops.write_file(str(target), "x\n", mode="create", developer_authorized=True)
    assert (policy_files.project / "okay.txt").read_text(encoding="utf-8") == "x\n"


# ---- the overlay's name prefix ---------------------------------------------------------

@pytest.mark.parametrize("developer", [False, True], ids=["no_token", "developer_token"])
@pytest.mark.parametrize("name", [
    "MODEL_TOOL_POLICY.json.bak", "model_tool_policy-x.json", "model_tool_policy", "Model_Tool_Policy.json.old",
])
def test_a_name_that_starts_like_the_overlay_is_refused(policy_files, name, developer):
    target = policy_files.overlay.with_name(name)
    with pytest.raises(PermissionError, match="protected|control"):
        file_ops.write_file(str(target), BODY, mode="create", developer_authorized=developer)
    assert not target.exists()
    # Control: another name in the same folder is not the overlay's.
    other = policy_files.overlay.with_name("scratch.json")
    file_ops.write_file(str(other), "{}\n", mode="create", developer_authorized=True)
    assert other.exists()


@pytest.mark.parametrize("developer", [False, True], ids=["no_token", "developer_token"])
def test_a_renamed_overlay_protects_names_that_start_with_its_stem(policy_files, monkeypatch, developer):
    renamed = policy_files.overlay.with_name("operator_grants.json")
    renamed.write_text('{"version": 1, "model_profiles": []}\n', encoding="utf-8")
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(renamed))
    sibling = renamed.with_name("operator_grants.json.bak")
    with pytest.raises(PermissionError, match="protected|control"):
        file_ops.write_file(str(sibling), BODY, mode="create", developer_authorized=developer)
    assert not sibling.exists()


# ---- reading the same file under another name ---------------------------------------------

# Preservation test, not a RED test: the read tools already refused this spelling on 73ee5116 (round-2
# probes only bypassed the write tools), so it passes there and here. It guards against a regression.
@windows_only
def test_an_alternate_data_stream_spelling_does_not_read_a_secret_file(policy_files):
    secret = policy_files.project / "server.pem"
    secret.write_text("-----BEGIN PRIVATE KEY-----\nSECRETKEYMATERIAL\n", encoding="utf-8")
    (policy_files.project / "app.py").write_text("print(1)\n", encoding="utf-8")
    plain = server.file_read_range(str(secret), 1, 5)
    assert "SECRETKEYMATERIAL" not in plain  # the baseline: the tool already refuses the plain name
    spelled = server.file_read_range(str(policy_files.project / "server.pem::$DATA"), 1, 5)
    assert "SECRETKEYMATERIAL" not in spelled, spelled
    assert spelled.startswith("ERROR"), spelled
    # Control: an ordinary file reads.
    assert "print(1)" in server.file_read_range(str(policy_files.project / "app.py"), 1, 5)


# ---- an archive cannot create the overlay ---------------------------------------------------

def test_archive_extract_cannot_create_a_missing_overlay(policy_files, tmp_path, monkeypatch):
    folder = tmp_path / "elsewhere"
    folder.mkdir()
    destination = folder / "sub"  # does not exist yet: the extraction makes it
    overlay = destination / "overlay.json"
    monkeypatch.setenv(mtp.OVERLAY_ENV, str(overlay))
    archive = tmp_path / "grant.zip"
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("overlay.json", BODY)
    out = server.archive_extract(str(archive), str(destination))
    assert out.startswith("ERROR"), out
    assert not overlay.exists()
    # Control: the same archive extracts to a folder that is not the overlay's.
    ordinary = folder / "ordinary"
    out = server.archive_extract(str(archive), str(ordinary))
    assert not out.startswith("ERROR"), out
    assert (ordinary / "overlay.json").read_text(encoding="utf-8") == BODY


# ---- hard links ---------------------------------------------------------------------------------

def _outside(tmp_path):
    outside = tmp_path / "user_project"
    outside.mkdir()
    return outside


def test_a_formatter_does_not_run_over_a_folder_holding_a_hard_link_to_the_overlay(policy_files, no_child, tmp_path):
    outside = _outside(tmp_path)
    link = outside / "config.json"
    os.link(policy_files.overlay, link)
    assert scope.scope_refusal("format_code", outside, outside)
    for call in (
        lambda: harness_tools.format_code(str(outside), tool="ruff"),
        lambda: harness_tools.lint_run(str(outside), tool="ruff", fix=True),
    ):
        result = call()
        assert result.get("ok") is False and "protected" in str(result.get("error", "")), result
    assert no_child == []
    # Control: with the other name gone, the same calls run the child.
    link.unlink()
    assert not scope.scope_refusal("format_code", outside, outside)
    assert harness_tools.format_code(str(outside), tool="ruff").get("ok") is True
    assert harness_tools.lint_run(str(outside), tool="ruff", fix=True).get("ok") is True
    assert len(no_child) == 2


def test_unrelated_hard_links_do_not_stop_formatting(policy_files, no_child, tmp_path):
    outside = _outside(tmp_path)
    (outside / "a.txt").write_text("same\n", encoding="utf-8")
    os.link(outside / "a.txt", outside / "b.txt")  # a pnpm store or a uv environment looks like this
    assert os.stat(outside / "a.txt").st_nlink == 2
    assert os.stat(policy_files.overlay).st_nlink == 1 and os.stat(policy_files.shipped).st_nlink == 1
    assert harness_tools.format_code(str(outside), tool="ruff").get("ok") is True
    assert harness_tools.lint_run(str(outside), tool="ruff", fix=True).get("ok") is True
    assert len(no_child) == 2


# ---- recursive delete with a developer token ------------------------------------------------------

def test_a_developer_recursive_delete_does_not_follow_a_link_into_the_policy_folders(policy_files, tmp_path):
    victim = tmp_path / "work"
    victim.mkdir()
    (victim / "a.txt").write_text("x\n", encoding="utf-8")
    _link_directory(victim / "j_data", policy_files.data)
    _link_directory(victim / "j_state", policy_files.overlay.parent)
    shipped_before = policy_files.shipped.read_bytes()
    overlay_before = policy_files.overlay.read_bytes()
    result = file_ops.delete_path(
        str(victim), recursive=True, dry_run=False, confirm="DELETE %s" % victim.resolve(),
        developer_authorized=True,
    )
    assert result.get("deleted") is True, result
    assert not victim.exists()
    assert policy_files.shipped.exists() and policy_files.overlay.exists()
    assert policy_files.shipped.read_bytes() == shipped_before
    assert policy_files.overlay.read_bytes() == overlay_before


def test_a_developer_recursive_delete_still_removes_an_ordinary_tree(policy_files, tmp_path):
    victim = tmp_path / "ordinary"
    (victim / "deep").mkdir(parents=True)
    (victim / "deep" / "a.txt").write_text("x\n", encoding="utf-8")
    result = file_ops.delete_path(
        str(victim), recursive=True, dry_run=False, confirm="DELETE %s" % victim.resolve(),
        developer_authorized=True,
    )
    assert result.get("deleted") is True, result
    assert not victim.exists()


# ---- case variants of the exact files (POSIX compares paths case-sensitively) --------------


def test_a_case_variant_of_a_policy_file_or_its_folder_is_still_a_policy_name(tmp_path):
    from sonder_runtime.adapters.filesystem.operator_only_paths import PolicyFiles

    shipped = tmp_path / "pkg" / "model_tool_policy.json"
    overlay = tmp_path / "state" / "model_tool_policy.json"
    files = PolicyFiles(shipped=shipped, overlay=overlay, state_dir=overlay.parent)
    assert files.names(Path(str(tmp_path)) / "PKG" / "MODEL_TOOL_POLICY.JSON")
    assert files.names(overlay.with_name("Model_Tool_Policy.json.LOCK"))
    assert files.under(Path(str(tmp_path)) / "Pkg")
    # Control: an unrelated name in the same folder is not a policy file.
    assert not files.names(tmp_path / "pkg" / "readme.json")
    assert not files.under(tmp_path / "elsewhere")
