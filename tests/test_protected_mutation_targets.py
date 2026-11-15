"""Every tool that changes files refuses the protected control-plane paths, however they are named.

``tests/test_model_tool_policy_protection.py`` covers the general file tools
(file_write, file_edit, file_copy) against the two model tool policy files by
their ordinary names.  This file covers the rest:

* other names for the same file: a hard link inside an allowed project, the
  Windows ``\\\\?\\`` namespace spelling, and an administrative-share UNC
  spelling of a local file;
* the other tools that write files: ``fetch_artifact`` (its destination, its
  staging file and its provenance sidecar), ``apply_patch`` (every path the
  patch names) and the git tools that change the working tree (checkout,
  branch with a base, stash push/pop/apply, merge, cherry-pick) when they run
  in the live runtime checkout.

The real shipped file is never the target: the package data directory is
pointed at a copy inside ``tmp_path``, so a missing guard changes the copy.
Every refusal is paired with a control showing that the same call works on an
ordinary file, so a refusal for another reason cannot pass these tests.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import harness_tools
import sonder_runtime.adapters.artifact_fetch as artifact_fetch
from sonder_runtime.adapters import model_tool_policy as mtp
from sonder_runtime.adapters.operator_attendance import attended
from sonder_runtime.adapters.filesystem import file_ops

from tests.support.policy_checkout import (
    FORGED,
    PROTECTED,
    windows_only,
)
from tests.support.policy_checkout import evil_branch as _evil_branch
from tests.support.policy_checkout import git as _git
from tests.support.policy_checkout import plain_branch as _plain_branch
from tests.support.policy_checkout import targets as _targets

pytest_plugins = ("tests.support.policy_checkout",)  # the policy_files and checkout fixtures


# ---- other names for the same file ------------------------------------------------

@pytest.mark.parametrize("which", ["shipped", "overlay"])
def test_a_hard_link_to_a_policy_file_cannot_be_written(policy_files, which):
    target = _targets(policy_files)[which]
    link = policy_files.project / "innocent.json"
    os.link(target, link)
    before = target.read_bytes()
    assert file_ops._is_protected_mutation_path(link)
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file(str(link), FORGED, mode="overwrite")
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file(str(link), FORGED, mode="append")
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.edit_file(str(link), '"version": 1', '"version": 1, "x": 1')
    with pytest.raises((PermissionError, file_ops.BatchWriteError)):
        file_ops.batch_write_files([{"path": str(link), "content": FORGED, "mode": "overwrite"}])
    assert target.read_bytes() == before
    # Control: an ordinary file beside the link is written.
    plain = policy_files.project / "plain.json"
    file_ops.write_file(str(plain), "{}\n")
    file_ops.batch_write_files([{"path": str(plain), "content": "[]\n", "mode": "overwrite"}])
    assert plain.read_text(encoding="utf-8") == "[]\n"


def test_any_hard_linked_file_is_not_written_in_place(policy_files):
    """A write through one name changes every name; the other name may be anywhere."""
    first = policy_files.project / "a.txt"
    first.write_text("one\n", encoding="utf-8")
    second = policy_files.repo / "elsewhere.txt"
    os.link(first, second)
    with pytest.raises(PermissionError, match="hard link"):
        file_ops.write_file(str(first), "two\n", mode="overwrite")
    assert second.read_text(encoding="utf-8") == "one\n"
    second.unlink()  # one name left: an ordinary file again
    file_ops.write_file(str(first), "two\n", mode="overwrite")
    assert first.read_text(encoding="utf-8") == "two\n"


@windows_only
@pytest.mark.parametrize("which", ["shipped", "overlay"])
def test_the_extended_namespace_spelling_of_a_policy_file_is_protected(policy_files, monkeypatch, tmp_path, which):
    target = _targets(policy_files)[which]
    spelled = "\\\\?\\" + str(target)
    monkeypatch.setenv("SONDER_FILE_ROOTS", "\\\\?\\" + str(tmp_path))
    before = target.read_bytes()
    assert file_ops._is_protected_mutation_path(Path(spelled))
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file(spelled, FORGED, mode="overwrite")
    assert target.read_bytes() == before
    # A new file in the package data directory, spelled the same way, is refused too.
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file("\\\\?\\" + str(policy_files.data / "new.json"), "{}")
    assert not (policy_files.data / "new.json").exists()
    # Control: an ordinary file in the same spelling is written.
    plain = "\\\\?\\" + str(policy_files.project / "plain.json")
    file_ops.write_file(plain, "{}\n")
    assert (policy_files.project / "plain.json").read_text(encoding="utf-8") == "{}\n"


@windows_only
def test_an_unsupported_windows_namespace_counts_as_protected():
    spelled = "\\\\?\\GLOBALROOT\\Device\\HarddiskVolume1\\x\\model_tool_policy.json"
    assert file_ops._is_protected_mutation_path(Path(spelled))


@windows_only
@pytest.mark.parametrize("which", ["shipped", "overlay"])
def test_an_administrative_share_spelling_of_a_policy_file_is_protected(policy_files, which):
    target = _targets(policy_files)[which]
    drive, rest = os.path.splitdrive(str(target))
    spelled = "\\\\localhost\\%s$%s" % (drive.rstrip(":"), rest)
    try:
        os.stat(spelled)
    except OSError:
        pytest.skip("the administrative share is not reachable on this host")
    assert file_ops._is_protected_mutation_path(Path(spelled))
    assert not file_ops._is_protected_mutation_path(
        Path("\\\\localhost\\%s$%s" % (drive.rstrip(":"), os.path.splitdrive(str(policy_files.project))[1]))
        / "plain.json"
    )


# ---- fetch_artifact ---------------------------------------------------------------

@pytest.fixture
def no_download(monkeypatch):
    calls = []

    def download(url, part, **kwargs):
        calls.append(Path(part))
        return {"ok": False, "checks": [{"name": "transport", "ok": False, "detail": "stubbed"}]}

    monkeypatch.setattr(artifact_fetch, "_web_tools", lambda: SimpleNamespace(enabled=lambda: True))
    monkeypatch.setattr(artifact_fetch, "_download", download)
    return calls


@pytest.mark.parametrize("case", ["destination", "staging", "sidecar"])
def test_fetch_artifact_never_writes_a_protected_path(policy_files, monkeypatch, no_download, case):
    folder = policy_files.repo / "state"
    if case == "destination":
        dest = policy_files.data / "model_tool_policy.json"
        protected = dest
    elif case == "staging":
        # ``<dest>.part<suffix>`` is the overlay.
        protected = folder / "b.json.part.json"
        monkeypatch.setenv(mtp.OVERLAY_ENV, str(protected))
        dest = folder / "b.json"
    else:
        # ``<dest>.provenance.json`` is the overlay.
        protected = folder / ("a" + artifact_fetch.PROVENANCE_SUFFIX)
        monkeypatch.setenv(mtp.OVERLAY_ENV, str(protected))
        dest = folder / "a"
    protected.write_text("{}\n", encoding="utf-8")
    before = protected.read_bytes()
    with pytest.raises(PermissionError, match=PROTECTED):
        artifact_fetch.fetch_artifact("https://example.invalid/x.bin", str(dest), overwrite=True)
    assert no_download == []
    assert protected.read_bytes() == before
    # Control: an ordinary destination reaches the download.
    artifact_fetch.fetch_artifact("https://example.invalid/x.bin", str(policy_files.project / "x.bin"))
    assert no_download == [policy_files.project / "x.bin.part.bin"]


# ---- apply_patch and the git tools ---------------------------------------------------

def _patch(relative, old, new):
    return (
        "--- a/{0}\n+++ b/{0}\n@@ -1 +1 @@\n-{1}\n+{2}\n".format(relative, old, new)
    )


@pytest.mark.parametrize("which", ["shipped", "overlay"])
def test_apply_patch_refuses_a_patch_that_changes_a_policy_file(checkout, which):
    """Even the operator's own ``/apply_patch`` is checked path by path (the attended call is not unchecked)."""
    with attended("apply_patch"):
        _apply_patch_refuses_a_policy_file(checkout, which)


def _apply_patch_refuses_a_policy_file(checkout, which):
    target = _targets(checkout)[which]
    relative = target.relative_to(checkout.repo).as_posix()
    first_line = target.read_text(encoding="utf-8").splitlines()[0]
    before = target.read_bytes()
    patch = _patch(relative, first_line, '{"version": 1, "model_profiles": [], "x": 1')
    result = harness_tools.apply_patch(str(checkout.repo), patch)
    assert result["ok"] is False and "protected" in result["error"], result
    assert target.read_bytes() == before
    # A patch that also touches an ordinary file is refused as a whole.
    both = _patch("hello.txt", "hello", "hi") + patch
    assert harness_tools.apply_patch(str(checkout.repo), both)["ok"] is False
    assert (checkout.repo / "hello.txt").read_text(encoding="utf-8") == "hello\n"
    # Control: the ordinary half alone applies.
    assert harness_tools.apply_patch(str(checkout.repo), _patch("hello.txt", "hello", "hi"))["ok"] is True
    assert (checkout.repo / "hello.txt").read_text(encoding="utf-8").startswith("hi")


def test_apply_patch_refuses_a_rename_onto_and_a_creation_of_a_protected_path(checkout):
    with attended("apply_patch"):
        _apply_patch_refuses_a_rename_and_a_creation(checkout)


def _apply_patch_refuses_a_rename_and_a_creation(checkout):
    checkout.shipped.unlink()
    rename = (
        "diff --git a/hello.txt b/pkg/data/model_tool_policy.json\n"
        "similarity index 100%\nrename from hello.txt\nrename to pkg/data/model_tool_policy.json\n"
    )
    result = harness_tools.apply_patch(str(checkout.repo), rename)
    assert result["ok"] is False and "protected" in result["error"], result
    create = (
        "--- /dev/null\n+++ b/pkg/data/extra.json\n@@ -0,0 +1 @@\n+{}\n"
    )
    result = harness_tools.apply_patch(str(checkout.repo), create)
    assert result["ok"] is False and "protected" in result["error"], result
    assert not checkout.shipped.exists() and not (checkout.data / "extra.json").exists()
    assert (checkout.repo / "hello.txt").exists()


def test_apply_patch_check_only_still_reports(checkout):
    relative = checkout.shipped.relative_to(checkout.repo).as_posix()
    first_line = checkout.shipped.read_text(encoding="utf-8").splitlines()[0]
    before = checkout.shipped.read_bytes()
    result = harness_tools.apply_patch(str(checkout.repo), _patch(relative, first_line, "{}"), check_only=True)
    assert "protected" not in str(result.get("error", ""))
    assert checkout.shipped.read_bytes() == before


def _unchanged(files, before):
    assert (files.shipped.read_bytes(), files.overlay.read_bytes()) == before


def _refused(result):
    assert result.get("ok") is False and "protected" in str(result.get("error", "")), result


@pytest.mark.parametrize("operation", ["checkout", "branch", "merge", "cherry_pick"])
def test_git_tools_refuse_to_change_a_policy_file_in_the_live_checkout(checkout, operation):
    """Unattended, no git tool rewrites the live checkout, whatever the change brings in."""
    sha = _evil_branch(checkout)
    plain = _plain_branch(checkout)
    before = (checkout.shipped.read_bytes(), checkout.overlay.read_bytes())
    root = str(checkout.repo)
    calls = {
        "checkout": (lambda: harness_tools.git_checkout(root, "evil"),
                     lambda: harness_tools.git_checkout(root, "plain")),
        "branch": (lambda: harness_tools.git_branch(root, "mine", base="evil"),
                   lambda: harness_tools.git_branch(root, "mine2", base="plain")),
        "merge": (lambda: harness_tools.git_merge(root, "evil"),
                  lambda: harness_tools.git_merge(root, "plain")),
        "cherry_pick": (lambda: harness_tools.git_cherry_pick(root, json.dumps([sha])),
                        lambda: harness_tools.git_cherry_pick(root, json.dumps([plain]))),
    }
    refuse, harmless = calls[operation]
    _refused(refuse())
    _refused(harmless())
    _unchanged(checkout, before)
    assert _head(checkout)[0] == "master"


def test_git_checkout_of_a_policy_file_path_is_refused(checkout):
    checkout.shipped.write_text(FORGED, encoding="utf-8")  # an operator's local edit
    _git(checkout.repo, "stash", "push", "-q")  # the forged content now sits in a stash
    _git(checkout.repo, "stash", "apply", "-q")
    before = (checkout.shipped.read_bytes(), checkout.overlay.read_bytes())
    _refused(harness_tools.git_checkout(str(checkout.repo), checkout.shipped.relative_to(checkout.repo).as_posix()))
    _unchanged(checkout, before)
    _refused(harness_tools.git_checkout(str(checkout.repo), "hello.txt"))


@pytest.mark.parametrize("action", ["pop", "push"])
def test_git_stash_refuses_to_change_a_policy_file_in_the_live_checkout(checkout, action):
    checkout.overlay.write_text(FORGED, encoding="utf-8")
    if action == "pop":
        _git(checkout.repo, "stash", "push", "-q")  # stashed outside the tools; the overlay is restored
    before = (checkout.shipped.read_bytes(), checkout.overlay.read_bytes())
    _refused(harness_tools.git_stash(str(checkout.repo), action=action))
    _unchanged(checkout, before)
    assert harness_tools.git_stash(str(checkout.repo), action="list")["ok"] is True


def test_git_stash_of_ordinary_changes_works_for_the_operator(checkout):
    (checkout.repo / "hello.txt").write_text("edited\n", encoding="utf-8")
    with attended("git_stash"):
        assert harness_tools.git_stash(str(checkout.repo), action="push", include_untracked=False)["ok"] is True
        assert harness_tools.git_stash(str(checkout.repo), action="pop")["ok"] is True
    assert (checkout.repo / "hello.txt").read_text(encoding="utf-8") == "edited\n"


def _head(files):
    branch = _git(files.repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    return branch, _git(files.repo, "rev-parse", "HEAD").stdout.strip()


def test_git_checkout_of_a_local_branch_switches_to_it_for_the_operator(checkout):
    plain = _plain_branch(checkout)
    with attended("git_checkout"):
        result = harness_tools.git_checkout(str(checkout.repo), "plain")
    assert result["ok"] is True, result
    assert _head(checkout) == ("plain", plain)


@pytest.mark.parametrize("which", ["shipped", "overlay"])
def test_rename_symbol_cannot_change_a_policy_file(checkout, which):
    target = _targets(checkout)[which]
    target.write_text(json.dumps({"version": 1, "model_profiles": [
        {"id": "operator-deny", "match": {"names": ["helper-model"], "prefixes": ["astra-cyber-"]},
         "tools": [], "lanes": []},
    ]}) + "\n", encoding="utf-8")
    (checkout.project / "mod.py").write_text("astra = 1\nhelper = 2\n", encoding="utf-8")
    before = target.read_bytes()
    glob = target.relative_to(checkout.repo).parent.as_posix() + "/*.json"
    for old in ("astra", "helper"):
        result = harness_tools.rename_symbol(str(checkout.repo), old, "zzzz", glob=glob, dry_run=False)
        assert result["ok"] is False and "protected" in result["error"], result
        assert target.read_bytes() == before
    # A rename that also reaches an ordinary file is refused as a whole.
    result = harness_tools.rename_symbol(str(checkout.repo), "astra", "zzzz", glob="**/*", dry_run=False)
    assert result["ok"] is False, result
    assert (checkout.project / "mod.py").read_text(encoding="utf-8") == "astra = 1\nhelper = 2\n"
    # The preview still works, and an ordinary rename is applied.
    assert harness_tools.rename_symbol(str(checkout.repo), "astra", "zzzz", glob=glob)["ok"] is True
    result = harness_tools.rename_symbol(str(checkout.repo), "astra", "zzzz", glob="proj/*.py", dry_run=False)
    assert result["ok"] is True and result["files_changed"] == 1, result
    assert (checkout.project / "mod.py").read_text(encoding="utf-8") == "zzzz = 1\nhelper = 2\n"


@pytest.fixture
def no_child(monkeypatch):
    calls = []

    def run(cmd, *, cwd, timeout=None, **kwargs):
        calls.append((list(cmd), Path(cwd)))
        return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}

    monkeypatch.setattr(harness_tools, "_run", run)
    return calls


@pytest.mark.parametrize("call, tool", [
    (lambda root, path: harness_tools.format_code(root, tool="ruff", path=path), "format_code"),
    (lambda root, path: harness_tools.lint_run(root, tool="ruff", path=path, fix=True), "lint_run"),
], ids=["format_code", "lint_run_fix"])
def test_formatters_and_lint_fixes_do_not_run_over_a_policy_file(checkout, no_child, call, tool):
    root = str(checkout.repo)
    for path in ("pkg/data/model_tool_policy.json", "pkg/data", "pkg", "state", "", "."):
        result = call(root, path)
        assert result.get("ok") is False and "protected" in str(result.get("error", "")), (path, result)
    assert no_child == []
    # Unattended, even an ordinary project directory in the live checkout is not formatted ...
    assert call(root, "proj").get("ok") is False
    assert no_child == []
    # ... control: for the operator at the console it is.
    with attended(tool):
        assert call(root, "proj").get("ok") is True
    assert len(no_child) == 1


def test_a_lint_check_without_fix_still_runs_over_a_policy_file(checkout, no_child):
    result = harness_tools.lint_run(str(checkout.repo), tool="ruff", path="pkg/data", fix=False)
    assert result.get("ok") is True and len(no_child) == 1


def test_a_formatter_does_not_follow_a_link_into_a_protected_directory(checkout, no_child, tmp_path):
    outside = tmp_path / "user_project"
    outside.mkdir()
    link = outside / "data_link"
    try:
        if sys.platform == "win32":
            subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(checkout.data)],
                           check=True, capture_output=True)
        else:
            link.symlink_to(checkout.data, target_is_directory=True)
    except (OSError, subprocess.CalledProcessError):
        pytest.skip("cannot create a directory link here")
    result = harness_tools.format_code(str(outside), tool="ruff")
    assert result.get("ok") is False and "protected" in str(result.get("error", "")), result
    assert no_child == []
    link.rmdir() if sys.platform == "win32" else link.unlink()
    assert harness_tools.format_code(str(outside), tool="ruff").get("ok") is True


# ---- a developer token does not open the policy files ---------------------------------

def _developer_writes(files, target):
    """Every general file tool, with a developer token, aimed at ``target``."""
    scratch = files.project / "forged.json"
    scratch.write_text(FORGED, encoding="utf-8")
    return {
        "write": lambda: file_ops.write_file(str(target), FORGED, mode="overwrite", developer_authorized=True),
        "append": lambda: file_ops.write_file(str(target), FORGED, mode="append", developer_authorized=True),
        "edit": lambda: file_ops.edit_file(str(target), '"version"', '"version_x"', developer_authorized=True),
        "copy_onto": lambda: file_ops.copy_file(str(scratch), str(target), overwrite=True, developer_authorized=True),
        "move_onto": lambda: file_ops.move_file(str(scratch), str(target), overwrite=True, developer_authorized=True),
        "move_away": lambda: file_ops.move_file(
            str(target), str(files.project / "moved.json"), developer_authorized=True,
        ),
        "delete": lambda: file_ops.delete_path(
            str(target), dry_run=False, confirm="DELETE %s" % target.resolve(), developer_authorized=True,
        ),
        "delete_folder": lambda: file_ops.delete_path(
            str(target.parent), recursive=True, dry_run=False,
            confirm="DELETE %s" % target.parent.resolve(), developer_authorized=True,
        ),
    }


@pytest.mark.parametrize("which", ["shipped", "overlay", "overlay_lock", "overlay_temp"])
@pytest.mark.parametrize("operation", [
    "write", "append", "edit", "copy_onto", "move_onto", "move_away", "delete", "delete_folder",
])
def test_a_developer_token_does_not_open_the_policy_files(policy_files, which, operation):
    targets = dict(_targets(policy_files))
    targets["overlay_lock"] = policy_files.overlay.with_name(policy_files.overlay.name + ".lock")
    targets["overlay_temp"] = policy_files.overlay.with_name(policy_files.overlay.name + ".tmp-0123456789abcdef")
    target = targets[which]
    if not target.exists():
        target.write_text('{"version": 1}\n', encoding="utf-8")
    snapshot = {path: path.read_bytes() for path in target.parent.iterdir() if path.is_file()}
    with pytest.raises(PermissionError, match=PROTECTED):
        _developer_writes(policy_files, target)[operation]()
    assert {path: path.read_bytes() for path in target.parent.iterdir() if path.is_file()} == snapshot
    assert not (policy_files.project / "moved.json").exists()


def test_a_developer_token_does_not_open_a_hard_link_to_a_policy_file(policy_files):
    link = policy_files.project / "innocent.json"
    os.link(policy_files.overlay, link)
    before = policy_files.overlay.read_bytes()
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file(str(link), FORGED, mode="overwrite", developer_authorized=True)
    assert policy_files.overlay.read_bytes() == before


def test_a_developer_token_still_writes_ordinary_files(policy_files):
    """Control: the developer token keeps opening every other path, protected code included."""
    plain = policy_files.overlay.with_name("plain.json")
    file_ops.write_file(str(plain), "{}\n", developer_authorized=True)
    assert plain.read_text(encoding="utf-8") == "{}\n"
    beside = policy_files.data / "other.json"
    file_ops.write_file(str(beside), "{}\n", developer_authorized=True)
    assert beside.read_text(encoding="utf-8") == "{}\n"
    with pytest.raises(PermissionError, match=PROTECTED):
        file_ops.write_file(str(beside), "[]\n", mode="overwrite")


def test_git_tools_outside_the_live_checkout_keep_their_behaviour(tmp_path, monkeypatch):
    """A user's repository holds no control-plane location: its git tools are unchanged."""
    monkeypatch.setenv("SONDER_FILE_ROOTS", str(tmp_path))
    repo = tmp_path / "user_repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "config", "user.email", "test@test.invalid")
    _git(repo, "config", "user.name", "Test")
    (repo / "server.pem").write_text("fixture one\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    _git(repo, "checkout", "-q", "-b", "other")
    (repo / "server.pem").write_text("fixture two\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", "rotate the test certificate")
    _git(repo, "checkout", "-q", "master")
    assert harness_tools.git_checkout(str(repo), "other")["ok"] is True
    assert (repo / "server.pem").read_text(encoding="utf-8") == "fixture two\n"
