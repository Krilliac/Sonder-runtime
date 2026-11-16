"""The live runtime checkout, and the rule that models do not change it.

Production is fast-forwarded from origin/main and self-modification has its
own reviewed pipeline, so a model-driven change to the checkout the runtime is
running from (a checkout, a branch switch, a merge, a stash, a patch, a
formatter) is never wanted: it could swap the shipped model tool policy, or any
other control-plane file, for another commit's.  Guessing which paths one
operation writes (and which commit a ref names) proved bypassable, so the rule
is structural: such a tool does not run in the live checkout unless the
operator typed that tool at the console (:mod:`operator_attendance`).

Which paths are live.  The question is not "which repository is nearest" but
"can git, run here, change the live checkout's files, ``HEAD``, index or
checked-out branch".  The set of paths called live must contain every path
from which git reaches the live repository; when the answer cannot be known,
the path is live.  A ``.git`` marker planted inside the live tree must not be
able to answer it, so:

* The *live roots* are every directory from a control-plane location (the
  runtime's source root, the package data directory, the operator overlay's
  folder, the Sonder home) up to the drive root that holds a ``.git`` entry,
  directory or file, valid or not.  Taking every one rather than the nearest
  means a planted marker can only add a root, never hide the real one.  A
  ``core.worktree`` in one of their git directories adds the tree it names.
  When the roots cannot be determined (a location cannot be named, or their
  git metadata cannot be read or parsed), every path is live and the refusal
  says why (:class:`LiveCheckoutUnknown`).
* (a) A path is live when it, or any directory above it, is the same
  directory (device and file index, so junctions, symlinks and case
  differences do not matter) as a live root, whatever markers sit in between.
  Both the spelling given and the resolved path are walked.
* (b) A path is also live when a git directory reachable from it or from a
  directory above it (a ``.git`` directory or junction; a ``.git`` file's
  ``gitdir:``, read as git reads it; the directory itself when it holds a
  ``HEAD``) could let git act on live state: it *is* a live git directory or
  common directory; its common directory is a live one, unless it is a
  registered worktree off the live branch (below); its ``refs`` is the live
  ``refs``; or ``core.worktree`` (in its ``config`` or ``config.worktree``, or
  its common directory's ``config``) names a tree inside a live root or
  containing one.  Every pointer file is read as present, absent or unknown
  (:mod:`git_metadata_reads`): an unknown read, or a ``stat`` error other than
  "no such file", makes the path live.
* (c) Git is asked as well (:mod:`live_checkout_git_probe`): a path is live
  when git, run there, names a live git directory, a live common directory
  (with the same worktree exception), or a work tree inside or containing a
  live root, or fails with anything but "not a git repository".  This runs
  only when (a) and (b) found the path not live.

A linked worktree of the live repository (``git worktree add`` outside the
live tree) on a branch the live checkout does not have checked out is not
live: it has its own ``HEAD``, index and files.  It qualifies only when its
git directory is ``<live common>/worktrees/<name>`` and that entry's ``gitdir``
file names, by file identity, the very ``.git`` file that led to it; then its
``HEAD`` (inside the live git directory) is followed as git follows it, shared
refs from the common directory, names compared without case.  Any other git
directory sharing the live common directory is live whatever its ``HEAD`` says,
so a planted ``HEAD`` text cannot make one look like a worktree.  A reftable
ref store, an unreadable ``HEAD`` or a live branch that cannot be read makes a
worktree live.  Worktrees share refs (branches, tags, the stash), which is why
``runtime_source_stash pop`` restores only a stash it recorded itself
(``git_tools``).

Not covered: a link planted deeper inside a git directory (``refs/heads`` as a
junction to the live one) needs a tool that creates links, which the file
tools do not; the live repository reached through a second mount (another
device id for the same files) is not recognised by identity, though git's
answer in (c) still names it.  Writing the live tree from an account that
cannot write it at all is the stronger enforcement and is outside this module.

* :func:`is_live` applies (a), (b) and (c).
* :func:`refusal` is the guard the tools call.

Nothing is cached; every call reads the disk as it is.  The functions hold no
state and may be called from any thread.  The cost is a few ``stat`` calls and
reads of small files (each at most ``git_metadata_reads.MAX_READ_BYTES``) per
directory between the path and its drive root, with no limit on that depth
(git walks to the root), plus, for a path (a) and (b) do not call live, two
short ``git rev-parse`` processes.  A check and the git run that follows it
are still two steps.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.git_metadata_reads as reads
import sonder_runtime.adapters.live_checkout_git_probe as git_probe
import sonder_runtime.adapters.model_tool_policy as _model_tool_policy
import sonder_runtime.adapters.operator_attendance as _attendance
from sonder_runtime.platform import paths as runtime_paths

_GIT_ENTRY = ".git"
_HEAD = "HEAD"
_COMMONDIR = "commondir"
_WORKTREES = "worktrees"
_BACKLINK = "gitdir"
_REFTABLE = "reftable"
_CONFIG_FILES = ("config", "config.worktree")


class LiveCheckoutGuard(str, Enum):
    """The ``guard`` field of a refused result."""

    LIVE_CHECKOUT = "live_checkout_attendance"


class LiveCheckoutUnknown(str, Enum):
    """Why the live checkout could not be located (every path is then live)."""

    LOCATION_UNAVAILABLE = "a control-plane location could not be named"
    METADATA_UNREADABLE = "the live checkout's git metadata could not be read"


class LiveCheckoutUnlocated(Exception):
    """The live roots could not be determined: ``cause``, at ``where``."""

    def __init__(self, cause: LiveCheckoutUnknown, where: str) -> None:
        super().__init__("%s (%s)" % (cause.value, where))
        self.cause = cause
        self.where = where


class _Unknown(Exception):
    """A read or ``stat`` whose answer cannot be known (``reason``, at ``path``)."""

    def __init__(self, reason: Enum | None, path: Path) -> None:
        super().__init__("%s: %s" % (reason.value if reason is not None else "unknown", path))
        self.reason = reason
        self.path = path


# ---- tri-state reads, raising _Unknown --------------------------------------------

def _status(path: Path) -> os.stat_result | None:
    """``os.stat`` of ``path``; None when it does not exist; raises :class:`_Unknown` on any other error."""
    status = reads.status_of(path)
    if status.state is reads.ReadState.UNKNOWN:
        raise _Unknown(status.unknown, path)
    return status.result


def _exists_unfollowed(path: Path) -> bool:
    status = reads.status_of(path, follow_links=False)
    if status.state is reads.ReadState.UNKNOWN:
        raise _Unknown(status.unknown, path)
    return status.state is reads.ReadState.OK


def _read(path: Path) -> bytes | None:
    read = reads.read_small(path)
    if read.state is reads.ReadState.UNKNOWN:
        raise _Unknown(read.unknown, path)
    return read.data if read.state is reads.ReadState.OK else None


def _pointer(path: Path, base: Path, *, prefix: bytes = b"") -> Path | None:
    """The path the pointer file at ``path`` names (relative to ``base``); None when absent or naming nothing."""
    data = _read(path)
    if data is None:
        return None
    pointer = reads.parse_pointer(data, prefix=prefix)
    if pointer.state is reads.ReadState.UNKNOWN:
        raise _Unknown(pointer.unknown, path)
    return reads.named_path(pointer.text, base) if pointer.state is reads.ReadState.OK else None


def _same(status: os.stat_result | None, others: Iterable[os.stat_result]) -> bool:
    return status is not None and any(os.path.samestat(status, other) for other in others)


def _ancestors(path: Path) -> Iterator[Path]:
    """``path`` and every directory above it, up to the drive root, with no count limit.

    Git walks to the root, so a bound here would leave a deep enough directory
    inside the live tree unchecked.  The walk ends because ``parent`` drops one
    component each step.
    """
    current = Path(path)
    while True:
        yield current
        parent = current.parent
        if parent == current:
            return
        current = parent


# ---- what git reads in a git directory ---------------------------------------------

@dataclass(frozen=True)
class _Found:
    """A git directory git can use from some directory, and the ``.git`` file that named it (None otherwise)."""

    git_dir: Path
    gitfile: Path | None


def _git_dirs_at(directory: Path) -> list[_Found]:
    """The git directory a ``.git`` entry in ``directory`` makes git use; none without one or for an invalid file."""
    entry = directory / _GIT_ENTRY
    status = _status(entry)
    if status is None:
        return []
    if stat.S_ISDIR(status.st_mode):
        return [_Found(entry, None)]
    if not stat.S_ISREG(status.st_mode):
        raise _Unknown(reads.Unknown.NOT_REGULAR, entry)
    named = _pointer(entry, directory, prefix=reads.GITFILE_PREFIX)
    return [] if named is None else [_Found(named, entry)]


def _common_dir(git_dir: Path) -> Path:
    """The common directory of ``git_dir``: what its ``commondir`` names (relative to it), else itself."""
    path = git_dir / _COMMONDIR
    data = _read(path)
    if data is None:
        return git_dir
    named = _pointer(path, git_dir)
    if named is None:
        raise _Unknown(reads.Unknown.UNPARSED, path)
    return named


def _configured_trees(git_dir: Path, common: Path) -> list[Path]:
    """Every tree ``core.worktree`` names for ``git_dir``: its config files and its common directory's.

    A relative value is taken against both directories, and ``~`` also
    expanded: more candidates only add refusals.
    """
    anchors = list(dict.fromkeys((git_dir, common)))
    trees: list[Path] = []
    for base in anchors:
        for name in _CONFIG_FILES:
            path = base / name
            found = reads.core_worktree_values(path)
            if found.state is reads.ReadState.UNKNOWN:
                raise _Unknown(found.unknown, path)
            for value in found.values:
                problem = reads.path_text_problem(value)
                if problem is not None:
                    raise _Unknown(problem, path)
                spellings = dict.fromkeys((value, os.path.expanduser(value)))
                trees.extend(reads.named_path(text, anchor) for text in spellings for anchor in anchors)
    return list(dict.fromkeys(trees))


def _uses_reftable(git_dir: Path, common: Path) -> bool:
    return any(_exists_unfollowed(base / _REFTABLE) for base in (git_dir, common))


# ---- the live set ------------------------------------------------------------------

@dataclass(frozen=True)
class _Branch:
    """The branch a live checkout has checked out (casefolded; None when detached), or not known."""

    known: bool
    ref: str | None = None


@dataclass(frozen=True)
class _Repository:
    """One live git directory: its identity, its common directory and ``refs``, and its branch."""

    git_dir: os.stat_result
    commons: tuple[os.stat_result, ...]
    refs: tuple[os.stat_result, ...]
    branch: _Branch


@dataclass(frozen=True)
class _Live:
    """The live roots and git directories, read once for one :func:`is_live` call."""

    roots: tuple[os.stat_result, ...]
    # Every directory at or above a live root: a work tree that is one of them contains live files.
    root_ancestors: tuple[os.stat_result, ...]
    repositories: tuple[_Repository, ...]

    @property
    def git_dirs(self) -> tuple[os.stat_result, ...]:
        return tuple(item for repo in self.repositories for item in (repo.git_dir, *repo.commons))


def _location_sources() -> tuple[tuple[str, Callable[[], object]], ...]:
    """Each control-plane location's name and the call that names it, looked up at call time."""
    return (
        ("workspace_root", file_ops.workspace_root),
        ("RUNTIME_PACKAGE_DATA_DIR", lambda: file_ops.RUNTIME_PACKAGE_DATA_DIR),
        ("overlay_path", _model_tool_policy.overlay_path),
        ("default_home", runtime_paths.default_home),
    )


def _control_locations() -> list[Path]:
    """Every control-plane location; raises :class:`LiveCheckoutUnlocated` when one cannot be named."""
    locations = []
    for name, source in _location_sources():
        try:
            locations.append(file_ops._resolve_best_effort(Path(source())))
        except Exception as exc:  # whatever the failure, that location's roots are unknown
            where = "%s: %s" % (name, type(exc).__name__)
            raise LiveCheckoutUnlocated(LiveCheckoutUnknown.LOCATION_UNAVAILABLE, where) from exc
    return locations


def _branch_of(git_dir: Path, common: Path) -> _Branch:
    if _uses_reftable(git_dir, common):
        return _Branch(known=False)
    followed = reads.followed_ref(git_dir, common)
    if followed.state is not reads.ReadState.OK:
        return _Branch(known=False)
    return _Branch(known=True, ref=followed.ref.casefold() if followed.ref else None)


def _repository(git_dir: Path) -> _Repository | None:
    status = _status(git_dir)
    if status is None or not stat.S_ISDIR(status.st_mode):
        return None
    common = _common_dir(git_dir)
    common_status = _status(common)
    refs_status = _status(common / "refs") if common_status is not None else None
    return _Repository(
        git_dir=status,
        commons=(common_status,) if common_status is not None else (),
        refs=(refs_status,) if refs_status is not None else (),
        branch=_branch_of(git_dir, common),
    )


def _existing(paths: Iterable[Path]) -> tuple[os.stat_result, ...]:
    return tuple(status for status in map(_status, paths) if status is not None)


def _live() -> _Live:
    """The live set; raises :class:`LiveCheckoutUnlocated` when it cannot be determined."""
    locations = _control_locations()
    try:
        roots: list[Path] = []
        for location in locations:
            roots.extend(directory for directory in _ancestors(location) if _exists_unfollowed(directory / _GIT_ENTRY))
        repositories: list[_Repository] = []
        trees: list[Path] = []
        for root in dict.fromkeys(roots):
            for found in _git_dirs_at(root):
                repository = _repository(found.git_dir)
                if repository is not None:
                    repositories.append(repository)
                    trees.extend(_configured_trees(found.git_dir, _common_dir(found.git_dir)))
        live_paths = list(dict.fromkeys((*roots, *trees)))
        root_status = _existing(live_paths)
        ancestors = _existing(dict.fromkeys(item for path in live_paths for item in _ancestors(path)))
    except _Unknown as exc:
        raise LiveCheckoutUnlocated(LiveCheckoutUnknown.METADATA_UNREADABLE, str(exc)) from exc
    return _Live(root_status, ancestors, tuple(repositories))


# ---- rule (a) and (b) ------------------------------------------------------------------

def _spellings(path: Path) -> list[Path]:
    """``path`` as given (made absolute, links kept) and resolved."""
    return list(dict.fromkeys((Path(os.path.abspath(path)), file_ops._resolve_best_effort(Path(path)))))


def _inside_live_root(path: Path, live: _Live) -> bool:
    return any(
        _same(_status(directory), live.roots)
        for spelled in _spellings(path) for directory in _ancestors(spelled)
    )


def _tree_reaches_live(tree: Path, live: _Live) -> bool:
    """Whether a work tree at ``tree`` is inside a live root or contains one."""
    return _same(_status(tree), live.root_ancestors) or _inside_live_root(tree, live)


def _registered_worktree_off_live_branch(found: _Found, common_status: os.stat_result,
                                         sharing: list[_Repository]) -> bool:
    """Whether ``found`` is a registered worktree of a live repository on a branch no live checkout has.

    Its git directory must be ``<live common>/worktrees/<name>`` and that
    entry's ``gitdir`` must name, by file identity, the ``.git`` file that led
    here.  Those files sit inside the live git directory; a planted git
    directory elsewhere never qualifies, whatever its ``HEAD`` holds.
    """
    git_dir, gitfile = found.git_dir, found.gitfile
    if gitfile is None or git_dir.parent.name.casefold() != _WORKTREES:
        return False
    registry_common = _status(git_dir.parent.parent)
    if registry_common is None or not os.path.samestat(registry_common, common_status):
        return False
    backlink = _pointer(git_dir / _BACKLINK, git_dir)
    if backlink is None:
        return False
    named, found_file = _status(backlink), _status(gitfile)
    if named is None or found_file is None or not os.path.samestat(named, found_file):
        return False
    branch = _branch_of(git_dir, git_dir.parent.parent)
    if not branch.known:
        return False
    for repository in sharing:
        if not repository.branch.known:
            return False
        if branch.ref is not None and branch.ref == repository.branch.ref:
            return False
    return True


def _shares_live_common(found: _Found, common: Path, live: _Live) -> bool:
    """Whether ``found``'s common directory is a live one and it is not a registered worktree off the live branch."""
    common_status = _status(common)
    if common_status is None:
        return False
    sharing = [repository for repository in live.repositories if _same(common_status, repository.commons)]
    return bool(sharing) and not _registered_worktree_off_live_branch(found, common_status, sharing)


def _uses_live_state(found: _Found, live: _Live) -> bool:
    """Whether git using ``found`` could act on a live checkout's ``HEAD``, index, branch or files."""
    status = _status(found.git_dir)
    if status is None or not stat.S_ISDIR(status.st_mode):
        return False  # git dies on a gitdir that is not a directory
    if _same(status, live.git_dirs):
        return True
    common = _common_dir(found.git_dir)
    if _shares_live_common(found, common, live):
        return True
    if _same(_status(common / "refs"), (item for repo in live.repositories for item in repo.refs)):
        common_status = _status(common)
        if not _same(common_status, (item for repo in live.repositories for item in repo.commons)):
            return True
    return any(_tree_reaches_live(tree, live) for tree in _configured_trees(found.git_dir, common))


def _directory_reaches_live(directory: Path, live: _Live) -> bool:
    """Rules (a) and (b) for one directory on the walk; raises :class:`_Unknown` when it cannot be known."""
    if _same(_status(directory), live.roots):
        return True
    candidates = _git_dirs_at(directory)
    head = _status(directory / _HEAD)
    if head is not None and stat.S_ISREG(head.st_mode):
        candidates.append(_Found(directory, None))
    return any(_uses_live_state(found, live) for found in candidates)


def _file_rule_live(path: Path, live: _Live) -> bool:
    """Rules (a) and (b): the path, or a directory above it, is or reaches live state, or cannot be known."""
    try:
        spellings = _spellings(path)
        return any(
            _directory_reaches_live(directory, live)
            for spelled in spellings for directory in _ancestors(spelled)
        )
    except (_Unknown, ValueError):
        return True


# ---- rule (c): git's own answer ------------------------------------------------------------

def _git_probe_live(path: Path, live: _Live) -> bool:
    """Rule (c): git, run at ``path``, names live state, or answers anything but "not a repository"."""
    answer = git_probe.probe(path)
    if answer.outcome in (git_probe.ProbeOutcome.NO_GIT, git_probe.ProbeOutcome.NOT_A_REPOSITORY):
        return False
    if answer.outcome is not git_probe.ProbeOutcome.FOUND or answer.location is None:
        return True
    location = answer.location
    try:
        if _same(_status(location.git_dir), live.git_dirs):
            return True
        gitfile = location.toplevel / _GIT_ENTRY if location.toplevel is not None else None
        if _shares_live_common(_Found(location.git_dir, gitfile), location.common_dir, live):
            return True
        return location.toplevel is not None and _tree_reaches_live(location.toplevel, live)
    except (_Unknown, ValueError):
        return True


# ---- the verdict -----------------------------------------------------------------------

@dataclass(frozen=True)
class _Verdict:
    live: bool
    unlocated: LiveCheckoutUnlocated | None = None


def _verdict(path: Path) -> _Verdict:
    try:
        live = _live()
    except LiveCheckoutUnlocated as exc:
        return _Verdict(True, exc)
    if not live.roots and not live.repositories:
        return _Verdict(False)
    return _Verdict(_file_rule_live(path, live) or _git_probe_live(path, live))


def is_live(path: Path) -> bool:
    """Whether git run at ``path`` could change the live runtime checkout (rules (a), (b) and (c) above)."""
    return _verdict(Path(path)).live


@dataclass(frozen=True)
class LiveCheckoutRefusal:
    """An unattended ``operation`` was refused in the live checkout.

    ``cause`` and ``where`` are set when the live checkout could not be
    located, so the refusal covered every path.
    """

    operation: str
    cause: LiveCheckoutUnknown | None = None
    where: str = ""

    @property
    def message(self) -> str:
        text = (
            "refusing %s: the live Sonder runtime checkout is protected; it changes only when the "
            "operator types the command at the console (no developer token opens it)" % self.operation
        )
        if self.cause is None:
            return text
        return text + "; the live checkout could not be located (%s: %s), so every path is treated as live" % (
            self.cause.value, self.where,
        )

    def as_result(self) -> dict:
        """The dict shape the ``harness_tools`` functions return for a refusal."""
        return {"ok": False, "error": self.message, "guard": LiveCheckoutGuard.LIVE_CHECKOUT.value}


def refusal(operation: str, path: Path) -> LiveCheckoutRefusal | None:
    """The refusal for ``operation`` run in ``path``, or None when it may run.

    It may run when the operator typed ``operation`` at the console, or when
    ``path`` is not live (:func:`is_live`).  Only a path rules (a) and (b) do
    not call live costs a ``git rev-parse`` (rule (c)).
    """
    if _attendance.attended_for(operation):
        return None
    verdict = _verdict(Path(path))
    if not verdict.live:
        return None
    if verdict.unlocated is None:
        return LiveCheckoutRefusal(operation)
    return LiveCheckoutRefusal(operation, verdict.unlocated.cause, verdict.unlocated.where)


__all__ = [
    "LiveCheckoutGuard", "LiveCheckoutRefusal", "LiveCheckoutUnknown", "LiveCheckoutUnlocated", "is_live",
    "refusal",
]
