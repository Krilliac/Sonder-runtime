"""Which paths are the live runtime checkout: what git will act on, not the nearest marker.

``live_checkout.is_live`` used to take the nearest directory holding a ``.git``
entry and compare it with the live checkout.  A developer-token holder can
write files under ``.git`` (the token skips the git-metadata check), and two
plain file writes then fooled it while git itself still acted on the live
repository:

* a nested ``proj/.git`` directory whose ``HEAD`` is not a ref: the guard saw
  a separate checkout, git skips the invalid marker and walks up to the live
  repository, so ``git_checkout(root=proj, ref="evil")`` switched the live
  ``HEAD`` and rewrote the shipped policy;
* a ``.git`` *file* reading ``gitdir: <live>/.git``, inside or outside the live
  tree: git uses the live ``HEAD`` and index with that folder as the working
  tree.

The rule now: (a) any path under the live root is live, whatever markers sit
in between, and the live root is every marked directory above a control-plane
location (so a marker planted between them only adds a root); (b) a git
directory reached from the path counts by what it shares with the live one
(identity, ``commondir`` plus the checked-out branch, ``refs``, or a
``core.worktree`` into the live tree).  A real worktree of the live repository,
and repositories outside the live tree, keep their behaviour.
"""
from __future__ import annotations

import pytest

import harness_tools
from sonder_runtime.adapters import live_checkout
from sonder_runtime.adapters.filesystem import file_ops
from tests.support.policy_checkout import evil_branch, git, plain_branch, windows_only

pytest_plugins = ("tests.support.policy_checkout",)  # the policy_files and checkout fixtures


def _head(repo):
    return git(repo, "rev-parse", "HEAD").stdout.strip()


def _branch(repo):
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()


def _refused(result):
    assert result.get("ok") is False and "protected" in str(result.get("error", "")), str(result)[:500]


def _developer_write(path, text):
    """The developer-token file write the attack uses (it is not refused under ``.git``)."""
    file_ops.write_file(str(path), text, mode="create", developer_authorized=True)


def _live_untouched(files, head, shipped):
    assert _head(files.repo) == head and _branch(files.repo) == "master"
    assert files.shipped.read_bytes() == shipped
    assert git(files.repo, "status", "--porcelain", "--untracked-files=no").stdout.strip() == ""


def _user_repo(path):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "master")
    git(path, "config", "user.email", "test@test.invalid")
    git(path, "config", "user.name", "Test")
    (path / "a.txt").write_text("a\n", encoding="utf-8")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "initial")
    git(path, "branch", "other")
    return path


# ---- (a) a marker inside the live tree does not make a separate checkout ---------------

def test_an_invalid_nested_marker_does_not_hide_the_live_checkout(checkout):
    """``proj/.git`` with ``HEAD`` "not a ref": git skips it and acts on the live repository."""
    evil_branch(checkout)
    head, shipped = _head(checkout.repo), checkout.shipped.read_bytes()
    proj = checkout.project
    _developer_write(proj / ".git" / "HEAD", "not a ref\n")
    _developer_write(proj / ".git" / "objects" / "keep", "")
    _developer_write(proj / ".git" / "refs" / "keep", "")
    # git agrees the marker is not a repository: it reports the live checkout for proj.
    assert git(proj, "rev-parse", "--show-toplevel").stdout.strip().casefold() == checkout.repo.as_posix().casefold()
    assert live_checkout.is_live(proj)
    _refused(harness_tools.git_checkout(str(proj), "evil"))
    _live_untouched(checkout, head, shipped)


def test_a_real_nested_repository_inside_the_live_tree_is_live(checkout):
    """A valid nested repository is still inside the live tree: an unattended checkout there is refused."""
    nested = _user_repo(checkout.project / "nested")
    assert live_checkout.is_live(nested)
    _refused(harness_tools.git_checkout(str(nested), "other"))
    assert _branch(nested) == "master"


@pytest.mark.parametrize("valid", [True, False], ids=["valid_marker", "invalid_marker"])
def test_a_marker_between_a_control_location_and_the_root_does_not_move_the_root(checkout, valid):
    """A ``.git`` made above each control location inside the tree must not become "the" live root.

    The fixture keeps the package data directory under ``pkg`` and the overlay under ``state``; a marker in
    both made the nearest-marker rule see two small checkouts and leave the real root unguarded.
    """
    evil_branch(checkout)
    head, shipped = _head(checkout.repo), checkout.shipped.read_bytes()
    for folder in (checkout.data.parent, checkout.overlay.parent):
        if valid:
            git(folder, "init", "-q", "-b", "master")
        else:
            _developer_write(folder / ".git" / "HEAD", "not a ref\n")
            _developer_write(folder / ".git" / "objects" / "keep", "")
            _developer_write(folder / ".git" / "refs" / "keep", "")
    assert live_checkout.is_live(checkout.repo)
    _refused(harness_tools.git_checkout(str(checkout.repo), "evil"))
    assert _head(checkout.repo) == head and checkout.shipped.read_bytes() == shipped


# ---- (b) a git directory reached from outside that is, or shares, the live one -------------

@pytest.mark.parametrize("where", ["inside", "outside", "outside_relative"])
def test_a_gitfile_naming_the_live_git_directory_is_live(checkout, tmp_path, where):
    evil_branch(checkout)
    head, shipped = _head(checkout.repo), checkout.shipped.read_bytes()
    folder = checkout.project / "linked" if where == "inside" else tmp_path / "outside"
    target = "../checkout/.git" if where == "outside_relative" else (checkout.repo / ".git").as_posix()
    _developer_write(folder / ".git", "gitdir: %s\n" % target)
    # git agrees: run in that folder it uses the live git directory.
    used = git(folder, "rev-parse", "--absolute-git-dir").stdout.strip()
    assert used.casefold() == (checkout.repo / ".git").as_posix().casefold()
    assert live_checkout.is_live(folder)
    _refused(harness_tools.git_checkout(str(folder), "evil"))
    _live_untouched(checkout, head, shipped)


def _planted_git_dir(files, tmp_path, head_text, name="planted"):
    """A git directory made of plain files that shares the live repository's refs through ``commondir``."""
    planted = tmp_path / name
    _developer_write(planted / "HEAD", head_text)
    _developer_write(planted / "commondir", (files.repo / ".git").as_posix() + "\n")
    folder = tmp_path / (name + "_tree")
    _developer_write(folder / ".git", "gitdir: %s\n" % planted.as_posix())
    return folder


@pytest.mark.parametrize("head_text", [
    "ref: refs/heads/master\n",
    pytest.param("ref: refs/heads/alias\n", id="through_a_symbolic_ref"),
    pytest.param("ref: refs/heads/MASTER\n", id="another_case", marks=windows_only),
])
def test_a_git_directory_following_the_live_branch_is_live(checkout, tmp_path, head_text):
    """A commit through it would move the live branch (git's "checked out elsewhere" check never runs)."""
    if "alias" in head_text:
        git(checkout.repo, "symbolic-ref", "refs/heads/alias", "refs/heads/master")
    folder = _planted_git_dir(checkout, tmp_path, head_text)
    assert live_checkout.is_live(folder)
    head = _head(checkout.repo)
    plain_branch(checkout)
    _refused(harness_tools.git_merge(str(folder), "plain", no_ff=False))
    assert _head(checkout.repo) == head


def test_a_git_directory_sharing_refs_on_another_branch_is_not_live(checkout, tmp_path):
    """Control: the same planted directory on a branch nobody has checked out is a worktree, not the live checkout."""
    git(checkout.repo, "branch", "side")
    folder = _planted_git_dir(checkout, tmp_path, "ref: refs/heads/side\n")
    assert not live_checkout.is_live(folder)


def test_a_core_worktree_into_the_live_tree_is_live(checkout, tmp_path):
    """``core.worktree`` in a git directory's own config sends git's working tree into the live checkout."""
    planted = tmp_path / "configured"
    git(tmp_path, "init", "-q", "--bare", str(planted))
    git(planted, "config", "core.bare", "false")
    git(planted, "config", "core.worktree", checkout.repo.as_posix())
    folder = tmp_path / "configured_tree"
    _developer_write(folder / ".git", "gitdir: %s\n" % planted.as_posix())
    assert git(folder, "rev-parse", "--show-toplevel").stdout.strip().casefold() == checkout.repo.as_posix().casefold()
    assert live_checkout.is_live(folder)
    # Run inside the git directory itself, git takes it as the repository and the same working tree.
    assert git(planted, "rev-parse", "--show-toplevel").stdout.strip().casefold() == checkout.repo.as_posix().casefold()
    assert live_checkout.is_live(planted)


# ---- worktrees and other repositories keep their behaviour ------------------------------------

def test_a_worktree_of_the_live_repository_is_not_live_until_its_head_follows_the_live_branch(checkout, tmp_path):
    plain = plain_branch(checkout)
    worktree = tmp_path / "wt"
    git(checkout.repo, "worktree", "add", "-q", "--detach", str(worktree), "master")
    assert not live_checkout.is_live(worktree)
    assert harness_tools.git_checkout(str(worktree), "plain")["ok"] is True
    assert _head(worktree) == plain
    git(worktree, "checkout", "-q", "--detach", "master")
    # A plain file write to the worktree's HEAD (no git check runs) makes it follow the live branch.
    worktree_git_dir = checkout.repo / ".git" / "worktrees" / "wt"
    (worktree_git_dir / "HEAD").write_text("ref: refs/heads/master\n", encoding="utf-8")
    assert live_checkout.is_live(worktree)
    head = _head(checkout.repo)
    _refused(harness_tools.git_merge(str(worktree), "plain", no_ff=False))
    assert _head(checkout.repo) == head


def test_a_nested_repository_outside_the_live_tree_keeps_every_git_tool(policy_files, tmp_path):
    outer = _user_repo(tmp_path / "user")
    nested = _user_repo(outer / "vendor" / "nested")
    for repo in (outer, nested):
        assert not live_checkout.is_live(repo)
        assert harness_tools.git_checkout(str(repo), "other")["ok"] is True
        assert _branch(repo) == "other"
