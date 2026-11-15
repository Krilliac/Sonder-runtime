"""The live-checkout rule fails closed: every audited fail-open path, checked against real git first.

Review cycle 2 found that ``live_checkout`` read pointer files through a
64 KiB cap that treated a larger file as absent, while git strips trailing
newlines: a ``.git`` file ``gitdir: <live>/.git`` padded with 70,000 newlines
let ``git_checkout`` switch the live branch, and a padded worktree ``HEAD`` let
a merge move live master.  An audit then listed 17 fail-open paths with git
behaviour quoted from memory.

Each test here builds one audit row in a temporary directory, against the
``checkout`` fixture standing in for the live repository, and first asserts
what the installed git actually does there (``rev-parse``, ``symbolic-ref``,
or the write itself).  Only then does it assert that ``is_live`` calls the
path live.  Where git does not behave as the audit claimed, the test asserts
git's real behaviour and says so.

``file_rule_only`` turns off git's own answer (rule (c)) so a row test proves
the file rule alone; :class:`TestGitAnswer` turns off the file rule and proves
rule (c) alone.  The end-to-end tests at the bottom run both.

Not built here: row 14 (the live repository through a second mount) needs a
second mount, and row 16 (``HEAD`` as a symbolic link) needs link creation,
which the file tools do not offer; dropping the hand-parsed branch carve-out
makes a planted ``HEAD`` irrelevant for row 16 anyway.
"""
from __future__ import annotations

import errno
import os
import subprocess
from pathlib import Path

import pytest

import harness_tools
import sonder_runtime.adapters.model_tool_policy as model_tool_policy
from sonder_runtime.adapters import live_checkout
from sonder_runtime.platform import paths as runtime_paths
from tests.support.policy_checkout import evil_branch, git, plain_branch, windows_only

pytest_plugins = ("tests.support.policy_checkout",)  # the policy_files and checkout fixtures

_PADDING = b"\n" * 70_000  # past the old 64 KiB cap
_GIT_ENV = {name: value for name, value in os.environ.items() if not name.upper().startswith("GIT_")}
_GIT_ENV["LC_ALL"] = "C"


def _git(cwd, *args, data=None):
    """Git with no ``GIT_*`` variables and the C locale; bytes in and out, never raises on failure."""
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, input=data, env=_GIT_ENV, check=False)


def _out(cwd, *args):
    result = _git(cwd, *args)
    assert result.returncode == 0, result.stderr.decode("utf-8", "replace")
    return result.stdout.decode("utf-8").strip()


def _git_dir(folder):
    return Path(_out(folder, "rev-parse", "--path-format=absolute", "--git-dir"))


def _common_dir(folder):
    return Path(_out(folder, "rev-parse", "--path-format=absolute", "--git-common-dir"))


def _toplevel(folder):
    return Path(_out(folder, "rev-parse", "--path-format=absolute", "--show-toplevel"))


def _same(first, second):
    return os.path.samestat(os.stat(first), os.stat(second))


def _write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _live_git(files):
    return files.repo / ".git"


def _posix(path):
    return Path(path).as_posix().encode("utf-8")


def _planted(files, tmp_path, head, name="planted"):
    """A git directory of plain files sharing the live common directory, and a folder whose ``.git`` names it."""
    planted = tmp_path / name
    _write(planted / "HEAD", head)
    _write(planted / "commondir", _posix(_live_git(files)) + b"\n")
    folder = tmp_path / (name + "_tree")
    _write(folder / ".git", b"gitdir: " + _posix(planted) + b"\n")
    return planted, folder


def _configured(tmp_path, name, config):
    """A bare repository whose config is ``config`` (bytes), and a folder whose ``.git`` names it."""
    repository = tmp_path / name
    _out(tmp_path, "init", "-q", "--bare", str(repository))
    _write(repository / "config", config)
    folder = tmp_path / (name + "_tree")
    _write(folder / ".git", b"gitdir: " + _posix(repository) + b"\n")
    return folder


def _core(lines):
    return b"[core]\n\trepositoryformatversion = 0\n\tbare = false\n" + lines


def _worktree(files, tmp_path, name="wt", *args):
    """A registered worktree of the live repository (``git worktree add``) and its git directory."""
    worktree = tmp_path / name
    _out(files.repo, "worktree", "add", "-q", *args, str(worktree), *([] if args else ["--detach", "master"]))
    return worktree, _live_git(files) / "worktrees" / name


def _master(files):
    return _out(files.repo, "rev-parse", "refs/heads/master")


def _refused(result):
    assert result.get("ok") is False and "protected" in str(result.get("error", "")), str(result)[:500]


@pytest.fixture
def file_rule_only(monkeypatch):
    """Rule (c), git's own answer, off: the test proves the file rule alone."""
    monkeypatch.setattr(live_checkout, "_git_probe_live", lambda path, live: False, raising=False)


@pytest.fixture
def reftable_checkout(policy_files):
    """The stand-in live checkout with a reftable ref store."""
    repo = policy_files.repo
    made = _git(repo, "init", "-q", "-b", "master", "--ref-format=reftable")
    if made.returncode != 0:
        pytest.skip("this git cannot make a reftable repository: %s" % made.stderr.decode("utf-8", "replace"))
    _out(repo, "config", "user.email", "test@test.invalid")
    _out(repo, "config", "user.name", "Test")
    (repo / "hello.txt").write_text("hello\n", encoding="utf-8")
    _out(repo, "add", "-A")
    _out(repo, "commit", "-q", "-m", "initial")
    return policy_files


# ---- the audit rows, file rule alone ------------------------------------------------------

@pytest.mark.usefixtures("file_rule_only")
class TestAuditRows:
    # Row 1: a pointer file over the old 64 KiB cap.
    @pytest.mark.parametrize("pointer", ["gitfile", "commondir", "planted_head", "config"])
    def test_row01_a_pointer_padded_past_the_old_cap_is_still_read(self, checkout, tmp_path, pointer):
        if pointer == "gitfile":
            folder = tmp_path / "outside"
            _write(folder / ".git", b"gitdir: " + _posix(_live_git(checkout)) + _PADDING)
            assert _same(_git_dir(folder), _live_git(checkout))
        elif pointer == "commondir":
            git(checkout.repo, "branch", "side")
            planted, folder = _planted(checkout, tmp_path, b"ref: refs/heads/side\n")
            _write(planted / "commondir", _posix(_live_git(checkout)) + _PADDING)
            assert _same(_common_dir(folder), _live_git(checkout))
        elif pointer == "planted_head":
            _, folder = _planted(checkout, tmp_path, b"ref: refs/heads/master" + _PADDING)
            assert _out(folder, "symbolic-ref", "HEAD") == "refs/heads/master"
        else:
            config = _core(b"#" * 70_000 + b"\n\tworktree = " + _posix(checkout.repo) + b"\n")
            folder = _configured(tmp_path, "padded_config", config)
            assert _same(_toplevel(folder), checkout.repo)
        assert live_checkout.is_live(folder)

    def test_row01_a_padded_registered_worktree_head_follows_the_live_branch(self, checkout, tmp_path):
        worktree, entry = _worktree(checkout, tmp_path)
        _write(entry / "HEAD", b"ref: refs/heads/master" + _PADDING)
        assert _out(worktree, "symbolic-ref", "HEAD") == "refs/heads/master"
        assert live_checkout.is_live(worktree)

    @pytest.mark.parametrize("pointer", ["commondir", "worktree_head", "config"])
    def test_row01_a_pointer_past_the_new_read_cap_is_unknown_hence_live(self, checkout, tmp_path, pointer):
        """Git reads ``commondir``, ``HEAD`` and config whole; over the 1 MiB read cap the rule must say live, not absent."""
        padding = b"\n" * (1024 * 1024 + 16)
        if pointer == "commondir":
            git(checkout.repo, "branch", "side")
            planted, folder = _planted(checkout, tmp_path, b"ref: refs/heads/side\n")
            _write(planted / "commondir", _posix(_live_git(checkout)) + padding)
            assert _same(_common_dir(folder), _live_git(checkout))
        elif pointer == "worktree_head":
            folder, entry = _worktree(checkout, tmp_path)
            _write(entry / "HEAD", b"ref: refs/heads/master" + padding)
            assert _out(folder, "symbolic-ref", "HEAD") == "refs/heads/master"
        else:
            config = _core(b"#" * (1024 * 1024 + 16) + b"\n\tworktree = " + _posix(checkout.repo) + b"\n")
            folder = _configured(tmp_path, "huge_config", config)
            assert _same(_toplevel(folder), checkout.repo)
        assert live_checkout.is_live(folder)

    def test_row01_a_config_past_the_read_cap_that_never_mentions_worktree_is_not_live(self, checkout, tmp_path):
        """Control: the large config is scanned, not refused outright."""
        folder = _configured(tmp_path, "huge_plain", _core(b"#" * (1024 * 1024 + 16) + b"\n"))
        assert not live_checkout.is_live(folder)

    def test_row01_git_dies_on_a_gitfile_over_one_mebibyte_and_the_rule_calls_it_live(self, checkout, tmp_path):
        """Git refuses a ``.git`` file over 1 MiB; the rule cannot read it either, so it is unknown, hence live."""
        folder = tmp_path / "huge"
        _write(folder / ".git", b"gitdir: " + _posix(_live_git(checkout)) + b"\n" * (1024 * 1024 + 16))
        result = _git(folder, "rev-parse", "--git-dir")
        assert result.returncode == 128 and b"too large to be a .git file" in result.stderr
        assert live_checkout.is_live(folder)

    # Row 2: whitespace, newlines included, after ``ref:``.
    def test_row02_a_symbolic_ref_split_over_two_lines_is_followed(self, checkout, tmp_path):
        _, folder = _planted(checkout, tmp_path, b"ref:\nrefs/heads/master\n")
        assert _out(folder, "symbolic-ref", "HEAD") == "refs/heads/master"
        assert live_checkout.is_live(folder)

    def test_row02_a_registered_worktree_head_split_over_two_lines_is_followed(self, checkout, tmp_path):
        worktree, entry = _worktree(checkout, tmp_path)
        _write(entry / "HEAD", b"ref:\n\trefs/heads/master  \n")
        assert _out(worktree, "symbolic-ref", "HEAD") == "refs/heads/master"
        assert live_checkout.is_live(worktree)

    # Row 3: shared refs come from the common directory, never the per-worktree one.
    def test_row03_a_decoy_loose_ref_in_a_planted_git_directory_is_ignored_as_git_ignores_it(self, checkout, tmp_path):
        planted, folder = _planted(checkout, tmp_path, b"ref: refs/heads/master\n")
        _write(planted / "refs" / "heads" / "master", b"ref: refs/heads/decoy\n")
        assert _out(folder, "rev-parse", "--symbolic-full-name", "HEAD") == "refs/heads/master"
        assert _out(folder, "rev-parse", "HEAD") == _master(checkout)
        assert live_checkout.is_live(folder)

    def test_row03_a_decoy_loose_ref_in_a_registered_worktree_is_ignored_as_git_ignores_it(self, checkout, tmp_path):
        worktree, entry = _worktree(checkout, tmp_path)
        _write(entry / "refs" / "heads" / "master", b"ref: refs/heads/decoy\n")
        _write(entry / "HEAD", b"ref: refs/heads/master\n")
        assert _out(worktree, "rev-parse", "--symbolic-full-name", "HEAD") == "refs/heads/master"
        assert live_checkout.is_live(worktree)

    # Row 4: a NUL byte; git stops reading the value there.
    @pytest.mark.parametrize("pointer", ["gitfile", "commondir", "worktree_head", "config"])
    def test_row04_a_nul_byte_after_a_live_pointer(self, checkout, tmp_path, pointer):
        live_git = _posix(_live_git(checkout))
        if pointer == "gitfile":
            folder = tmp_path / "with_nul"  # not "nul": a Windows device name
            _write(folder / ".git", b"gitdir: " + live_git + b"\0junk\n")
            assert _same(_git_dir(folder), _live_git(checkout))
        elif pointer == "commondir":
            git(checkout.repo, "branch", "side")
            planted, folder = _planted(checkout, tmp_path, b"ref: refs/heads/side\n")
            _write(planted / "commondir", live_git + b"\0junk\n")
            assert _same(_common_dir(folder), _live_git(checkout))
        elif pointer == "worktree_head":
            folder, entry = _worktree(checkout, tmp_path)
            _write(entry / "HEAD", b"ref: refs/heads/master\0junk\n")
            assert _out(folder, "symbolic-ref", "HEAD") == "refs/heads/master"
        else:
            folder = _configured(tmp_path, "nul_config", _core(b"\tworktree = " + _posix(checkout.repo) + b"\0junk\n"))
            assert _same(_toplevel(folder), checkout.repo)
        assert live_checkout.is_live(folder)

    # Row 5: an embedded newline in a pointer.
    def test_row05_an_embedded_newline_in_a_gitfile(self, checkout, tmp_path):
        """Git keeps the whole value; on Windows no such directory can exist, so git dies (negative evidence)."""
        folder = tmp_path / "newline"
        _write(folder / ".git", b"gitdir: " + _posix(_live_git(checkout)) + b"\nsecond\n")
        result = _git(folder, "rev-parse", "--git-dir")
        if os.name == "nt":
            assert result.returncode == 128 and b"not a git repository" in result.stderr
        assert live_checkout.is_live(folder)

    # Row 6: configs a line-based reader splits or trims differently from git.
    @pytest.mark.parametrize("form", [
        "bom", "continued_value", "continued_into_a_header", "two_headers", "form_feed", "lone_cr",
        "vertical_tab", "file_separator", "next_line", "line_separator", "nbsp",
    ])
    def test_row06_a_core_worktree_git_reads_and_a_line_reader_would_miss(self, checkout, tmp_path, form):
        live = _posix(checkout.repo)
        configs = {
            "bom": b"\xef\xbb\xbf" + _core(b"\tworktree = " + live + b"\n"),
            "continued_value": _core(b"\tworktree = " + live[:-3] + b"\\\n" + live[-3:] + b"\n"),
            "continued_into_a_header": _core(b"\tx = a\\\n[other]\n\tworktree = " + live + b"\n"),
            "two_headers": _core(b"[x][core] worktree = " + live + b"\n"),
            "form_feed": _core(b"\tx = a\x0c[y]\n\tworktree = " + live + b"\n"),
            "lone_cr": _core(b"\tx = a\r[y]\n\tworktree = " + live + b"\n"),
            "vertical_tab": _core(b"\tx = a\x0b[y]\n\tworktree = " + live + b"\n"),
            "file_separator": _core(b"\tx = a\x1c[y]\n\tworktree = " + live + b"\n"),
            "next_line": _core(b"\tx = a\xc2\x85[y]\n\tworktree = " + live + b"\n"),
            "line_separator": _core(b"\tx = a\xe2\x80\xa8[y]\n\tworktree = " + live + b"\n"),
            # Relative to the git directory, through a component named NBSP.
            "nbsp": _core(b"\tworktree = \xc2\xa0/../../checkout\n"),
        }
        folder = _configured(tmp_path, "c_" + form, configs[form])
        assert _same(_toplevel(folder), checkout.repo)
        assert live_checkout.is_live(folder)

    # Row 7: core.worktree in the common directory's config.
    def test_row07_core_worktree_in_the_common_directory_config(self, checkout, tmp_path):
        common = tmp_path / "attacker_common"
        _out(tmp_path, "init", "-q", "--bare", str(common))
        _write(common / "config", b"[core]\n\trepositoryformatversion = 1\n\tbare = false\n\tworktree = "
               + _posix(checkout.repo) + b"\n[extensions]\n\tworktreeConfig = true\n")
        planted = tmp_path / "planted"
        _write(planted / "HEAD", b"ref: refs/heads/master\n")
        _write(planted / "commondir", _posix(common) + b"\n")
        folder = tmp_path / "planted_tree"
        _write(folder / ".git", b"gitdir: " + _posix(planted) + b"\n")
        assert _same(_toplevel(folder), checkout.repo)
        assert live_checkout.is_live(folder)

    def test_row07_without_worktree_config_git_ignores_the_common_core_worktree(self, checkout, tmp_path):
        """Negative evidence for the audit's narrower case: no ``worktreeConfig``, no effect (the rule still refuses)."""
        common = tmp_path / "attacker_common"
        _out(tmp_path, "init", "-q", "--bare", str(common))
        _write(common / "config", _core(b"\tworktree = " + _posix(checkout.repo) + b"\n"))
        planted = tmp_path / "planted"
        _write(planted / "HEAD", b"ref: refs/heads/master\n")
        _write(planted / "commondir", _posix(common) + b"\n")
        folder = tmp_path / "planted_tree"
        _write(folder / ".git", b"gitdir: " + _posix(planted) + b"\n")
        assert _same(_toplevel(folder), folder)
        assert live_checkout.is_live(folder)

    # Row 8: a work tree that contains the live root.
    def test_row08_a_work_tree_above_the_live_checkout_is_live(self, checkout, tmp_path):
        folder = _configured(tmp_path, "above", _core(b"\tworktree = " + _posix(tmp_path) + b"\n"))
        assert _same(_toplevel(folder), tmp_path)
        assert live_checkout.is_live(folder)
        # And git would write live files through it: a hard reset to a commit holding checkout/hello.txt.
        blob = _git(folder, "hash-object", "-w", "--stdin", data=b"PWNED\n").stdout.decode().strip()
        _out(folder, "update-index", "--add", "--cacheinfo", "100644,%s,checkout/hello.txt" % blob)
        _out(folder, "-c", "user.name=a", "-c", "user.email=a@a.invalid", "commit", "-q", "-m", "evil")
        _out(folder, "reset", "-q", "--hard", "HEAD")
        assert (checkout.repo / "hello.txt").read_text(encoding="utf-8") == "PWNED\n"

    # Row 9: ref names compared without case.
    @windows_only
    def test_row09_a_registered_worktree_head_naming_master_in_another_case(self, checkout, tmp_path):
        worktree, entry = _worktree(checkout, tmp_path)
        _write(entry / "HEAD", b"ref: refs/heads/MASTER\n")
        assert _out(worktree, "rev-parse", "HEAD") == _master(checkout)
        assert live_checkout.is_live(worktree)

    # Row 10: Windows drive-relative and rooted pointers.
    @windows_only
    def test_row10_a_drive_relative_gitfile_resolved_against_the_working_directory(self, checkout, tmp_path):
        folder = tmp_path / "a" / "b"
        inner = folder / "c"
        inner.mkdir(parents=True)
        relative = os.path.relpath(_live_git(checkout), inner)
        _write(folder / ".git", b"gitdir: " + tmp_path.drive.encode() + relative.encode() + b"\n")
        assert _same(_git_dir(inner), _live_git(checkout))
        assert live_checkout.is_live(inner)

    @windows_only
    def test_row10_a_gitfile_rooted_without_a_drive(self, checkout, tmp_path):
        folder = tmp_path / "rooted"
        _write(folder / ".git", b"gitdir: " + _posix(_live_git(checkout))[2:] + b"\n")
        assert _same(_git_dir(folder), _live_git(checkout))
        assert live_checkout.is_live(folder)

    # Row 11: bytes that are not UTF-8.
    @windows_only
    def test_row11_a_gitfile_with_a_latin1_byte_naming_a_planted_git_directory(self, checkout, tmp_path):
        git(checkout.repo, "branch", "side")
        planted = tmp_path / "café"
        _write(planted / "HEAD", b"ref: refs/heads/side\n")
        _write(planted / "commondir", _posix(_live_git(checkout)) + b"\n")
        folder = tmp_path / "latin1"
        _write(folder / ".git", b"gitdir: " + _posix(tmp_path) + b"/caf\xe9\n")
        # Git for Windows reads the lone 0xE9 as U+00E9 and finds the planted directory.
        assert _same(_common_dir(folder), _live_git(checkout))
        assert live_checkout.is_live(folder)

    def test_row11_a_gitfile_with_a_latin1_byte_before_dot_dot(self, checkout, tmp_path):
        folder = tmp_path / "latin1_dots"
        _write(folder / ".git", b"gitdir: " + _posix(tmp_path) + b"/caf\xe9/../checkout/.git\n")
        if os.name == "nt":
            assert _same(_git_dir(folder), _live_git(checkout))
        assert live_checkout.is_live(folder)

    # Row 12: stat errors other than "no such file".
    def test_row12_an_error_reading_the_live_git_entry_makes_every_path_live(self, checkout, tmp_path, monkeypatch):
        denied = _deny_stat(monkeypatch, _live_git(checkout))
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        assert live_checkout.is_live(elsewhere)
        refused = live_checkout.refusal("git_checkout", elsewhere)
        assert refused is not None and "could not be located" in refused.message and "protected" in refused.message
        assert denied, "the denied path was never examined"

    def test_row12_an_error_reading_a_candidate_git_entry_is_live(self, checkout, tmp_path, monkeypatch):
        folder = tmp_path / "unreadable"
        _write(folder / ".git", b"gitdir: " + _posix(tmp_path / "anything") + b"\n")
        denied = _deny_stat(monkeypatch, folder / ".git")
        assert live_checkout.is_live(folder)
        assert denied, "the denied path was never examined"

    # Row 13: a control-plane location that cannot be named.
    @pytest.mark.parametrize("location", ["overlay_path", "default_home"])
    def test_row13_a_location_that_raises_makes_every_path_live_with_its_reason(
            self, checkout, tmp_path, monkeypatch, location):
        def broken():
            raise OSError(errno.EIO, "location unavailable")

        owner = model_tool_policy if location == "overlay_path" else runtime_paths
        monkeypatch.setattr(owner, location, broken)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        assert live_checkout.is_live(elsewhere)
        refused = live_checkout.refusal("git_checkout", elsewhere)
        assert refused is not None and location in refused.message
        assert refused.as_result()["guard"] == live_checkout.LiveCheckoutGuard.LIVE_CHECKOUT.value

    # Row 15: a reftable ref store.
    def test_row15_a_planted_git_directory_sharing_a_reftable_live_store_is_live(self, reftable_checkout, tmp_path):
        files = reftable_checkout
        assert (_live_git(files) / "HEAD").read_bytes() == b"ref: refs/heads/.invalid\n"
        _, folder = _planted(files, tmp_path, _master(files).encode() + b"\n")
        assert _same(_common_dir(folder), _live_git(files))
        assert live_checkout.is_live(folder)

    def test_row15_a_registered_worktree_of_a_reftable_store_is_live(self, reftable_checkout, tmp_path):
        """Its ``HEAD`` file is a stub, so its branch cannot be read without git: unknown, hence live."""
        files = reftable_checkout
        _out(files.repo, "branch", "side")
        worktree, entry = _worktree_on(files, tmp_path, "side")
        assert (entry / "HEAD").read_bytes() == b"ref: refs/heads/.invalid\n"
        assert live_checkout.is_live(worktree)

    # Row 17: a planted git directory naming a ref outside refs/heads, or detached.
    def test_row17_a_planted_git_directory_on_a_remote_tracking_ref_is_live(self, checkout, tmp_path):
        _out(checkout.repo, "update-ref", "refs/remotes/origin/main", "HEAD")
        _, folder = _planted(checkout, tmp_path, b"ref: refs/remotes/origin/main\n")
        assert live_checkout.is_live(folder)
        # A commit there moves the live repository's origin/main.
        before = _out(checkout.repo, "rev-parse", "refs/remotes/origin/main")
        (folder / "f.txt").write_text("x\n", encoding="utf-8")
        _out(folder, "add", "f.txt")
        _out(folder, "-c", "user.name=a", "-c", "user.email=a@a.invalid", "commit", "-q", "-m", "x")
        assert _out(checkout.repo, "rev-parse", "refs/remotes/origin/main") != before

    def test_row17_a_detached_planted_git_directory_sharing_the_live_store_is_live(self, checkout, tmp_path):
        _, folder = _planted(checkout, tmp_path, _master(checkout).encode() + b"\n")
        assert _same(_common_dir(folder), _live_git(checkout))
        assert live_checkout.is_live(folder)


def _worktree_on(files, tmp_path, branch, name="wt"):
    worktree = tmp_path / name
    _out(files.repo, "worktree", "add", "-q", str(worktree), branch)
    return worktree, _live_git(files) / "worktrees" / name


def _deny_stat(monkeypatch, target):
    """``os.stat`` and ``os.lstat`` raise ``EACCES`` for ``target``; returns the list of denied calls."""
    real_stat, real_lstat = os.stat, os.lstat
    key = os.path.normcase(os.path.abspath(target))
    denied = []

    def wrap(real):
        def checked(path, *args, **kwargs):
            if isinstance(path, (str, os.PathLike)) and os.path.normcase(os.path.abspath(os.fspath(path))) == key:
                denied.append(path)
                raise PermissionError(errno.EACCES, "access denied", os.fspath(path))
            return real(path, *args, **kwargs)
        return checked

    monkeypatch.setattr(os, "stat", wrap(real_stat))
    monkeypatch.setattr(os, "lstat", wrap(real_lstat))
    return denied


# ---- registered worktrees: the one case that is not live ---------------------------------------

@pytest.mark.usefixtures("file_rule_only")
class TestRegisteredWorktrees:
    def test_a_registered_worktree_on_its_own_branch_is_not_live(self, checkout, tmp_path):
        worktree, _ = _worktree(checkout, tmp_path, "wt", "-b", "feature")
        assert _out(worktree, "symbolic-ref", "HEAD") == "refs/heads/feature"
        assert not live_checkout.is_live(worktree)
        assert not live_checkout.is_live(worktree / "sub" / "missing")

    def test_a_registered_worktree_with_relative_links_on_its_own_branch_is_not_live(self, checkout, tmp_path):
        worktree = tmp_path / "relative"
        made = _git(checkout.repo, "-c", "worktree.useRelativePaths=true", "worktree", "add", "-q", "-b", "rel",
                    str(worktree))
        if made.returncode != 0:
            pytest.skip("this git cannot write relative worktree links")
        link = (worktree / ".git").read_bytes().decode("utf-8").removeprefix("gitdir: ").strip()
        assert not Path(link).is_absolute(), link
        assert not live_checkout.is_live(worktree)

    def test_a_registered_worktree_on_the_live_branch_is_live(self, checkout, tmp_path):
        worktree, _ = _worktree(checkout, tmp_path, "forced", "--detach")
        _out(worktree, "checkout", "-q", "--ignore-other-worktrees", "master")
        assert _out(worktree, "symbolic-ref", "HEAD") == "refs/heads/master"
        assert live_checkout.is_live(worktree)

    def test_a_gitfile_elsewhere_naming_a_registered_worktree_entry_is_live(self, checkout, tmp_path):
        """The entry's back-link names the worktree's own ``.git`` file, not this one: not a worktree, so live."""
        worktree, entry = _worktree(checkout, tmp_path, "wt", "-b", "feature")
        folder = tmp_path / "impostor"
        _write(folder / ".git", b"gitdir: " + _posix(entry) + b"\n")
        assert _same(_git_dir(folder), entry)
        assert not live_checkout.is_live(worktree)
        assert live_checkout.is_live(folder)

    def test_a_worktree_whose_live_branch_cannot_be_read_is_live(self, checkout, tmp_path):
        worktree, _ = _worktree(checkout, tmp_path, "wt", "-b", "feature")
        _write(_live_git(checkout) / "HEAD", b"ref: refs/heads/master" + b"\n" * (1024 * 1024 + 16))
        assert live_checkout.is_live(worktree)


# ---- rule (c): git's own answer, file rule off ------------------------------------------------------

@pytest.fixture
def git_answer_only(monkeypatch):
    monkeypatch.setattr(live_checkout, "_file_rule_live", lambda path, live: False)


@pytest.mark.usefixtures("git_answer_only")
class TestGitAnswer:
    @pytest.mark.parametrize("scenario", ["padded_gitfile", "nul_gitfile", "bom_config", "common_config", "above"])
    def test_git_naming_live_state_is_live(self, checkout, tmp_path, scenario):
        live = _posix(checkout.repo)
        if scenario == "padded_gitfile":
            folder = tmp_path / "padded"
            _write(folder / ".git", b"gitdir: " + live + b"/.git" + _PADDING)
        elif scenario == "nul_gitfile":
            folder = tmp_path / "with_nul"  # not "nul": a Windows device name
            _write(folder / ".git", b"gitdir: " + live + b"/.git\0junk\n")
        elif scenario == "bom_config":
            folder = _configured(tmp_path, "bom", b"\xef\xbb\xbf" + _core(b"\tworktree = " + live + b"\n"))
        elif scenario == "common_config":
            common = tmp_path / "attacker_common"
            _out(tmp_path, "init", "-q", "--bare", str(common))
            _write(common / "config", b"[core]\n\trepositoryformatversion = 1\n\tbare = false\n\tworktree = "
                   + live + b"\n[extensions]\n\tworktreeConfig = true\n")
            planted = tmp_path / "planted"
            _write(planted / "HEAD", b"ref: refs/heads/master\n")
            _write(planted / "commondir", _posix(common) + b"\n")
            folder = tmp_path / "planted_tree"
            _write(folder / ".git", b"gitdir: " + _posix(planted) + b"\n")
        else:
            folder = _configured(tmp_path, "above", _core(b"\tworktree = " + _posix(tmp_path) + b"\n"))
        assert live_checkout.is_live(folder)

    def test_git_naming_a_planted_git_directory_on_the_live_common_is_live(self, checkout, tmp_path):
        git(checkout.repo, "branch", "side")
        _, folder = _planted(checkout, tmp_path, b"ref: refs/heads/side\n")
        assert live_checkout.is_live(folder)

    def test_git_failing_with_another_message_is_live(self, checkout, tmp_path):
        folder = tmp_path / "invalid"
        _write(folder / ".git", b"not a pointer\n")
        result = _git(folder, "rev-parse", "--git-dir")
        assert result.returncode == 128 and b"invalid gitfile format" in result.stderr
        assert live_checkout.is_live(folder)

    def test_a_registered_worktree_off_the_live_branch_is_not_live(self, checkout, tmp_path):
        worktree, _ = _worktree(checkout, tmp_path, "wt", "-b", "feature")
        assert not live_checkout.is_live(worktree)
        _out(worktree, "checkout", "-q", "--ignore-other-worktrees", "master")
        assert live_checkout.is_live(worktree)

    def test_another_repository_and_no_repository_are_not_live(self, checkout, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        _out(other, "init", "-q")
        plain = tmp_path / "plain"
        plain.mkdir()
        assert not live_checkout.is_live(other)
        assert not live_checkout.is_live(plain)


# ---- end to end: both rules, the real tools --------------------------------------------------------

def test_the_padded_gitfile_attack_is_refused(checkout, tmp_path):
    """``attack_padded.py``: before the fix ``git_checkout`` here switched the live branch to ``evil``."""
    evil_branch(checkout)
    head, shipped = _master(checkout), checkout.shipped.read_bytes()
    outside = tmp_path / "outside"
    _write(outside / ".git", b"gitdir: " + _posix(_live_git(checkout)) + _PADDING)
    assert _same(_git_dir(outside), _live_git(checkout))
    assert live_checkout.is_live(outside)
    _refused(harness_tools.git_checkout(root=str(outside), ref="evil"))
    assert _out(checkout.repo, "symbolic-ref", "HEAD") == "refs/heads/master"
    assert _master(checkout) == head and checkout.shipped.read_bytes() == shipped


def test_a_padded_worktree_head_cannot_move_live_master(checkout, tmp_path):
    plain_branch(checkout)
    head = _master(checkout)
    worktree, entry = _worktree(checkout, tmp_path)
    _write(entry / "HEAD", b"ref: refs/heads/master" + _PADDING)
    _refused(harness_tools.git_merge(str(worktree), "plain", no_ff=False))
    assert _master(checkout) == head


def test_a_registered_worktree_on_its_own_branch_keeps_the_git_tools(checkout, tmp_path):
    plain = plain_branch(checkout)
    worktree, _ = _worktree(checkout, tmp_path, "wt", "-b", "feature")
    assert not live_checkout.is_live(worktree)
    assert harness_tools.git_merge(str(worktree), "plain", no_ff=False)["ok"] is True
    assert _out(worktree, "rev-parse", "HEAD") == plain
    assert harness_tools.git_checkout(str(worktree), "plain")["ok"] is True
    assert _out(checkout.repo, "symbolic-ref", "HEAD") == "refs/heads/master"
    assert _master(checkout) != plain


def test_git_is_asked_only_when_the_file_rule_says_not_live(checkout, tmp_path, monkeypatch):
    calls = []
    real = subprocess.run

    def recording(argv, *args, **kwargs):
        calls.append(list(argv))
        return real(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    assert live_checkout.is_live(checkout.project)
    assert calls == []
    other = tmp_path / "other"
    other.mkdir()
    assert not live_checkout.is_live(other)
    assert [argv[1:3] for argv in calls] == [["rev-parse", "--path-format=absolute"]]

