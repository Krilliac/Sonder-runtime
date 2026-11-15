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
checked-out branch".  A ``.git`` marker planted inside the live tree must not
be able to answer it, so:

* The *live roots* are every directory from a control-plane location (the
  runtime's source root, the package data directory, the operator overlay's
  folder, the Sonder home) up to the drive root that holds a ``.git`` entry,
  directory or file, valid or not.  Taking every one rather than the nearest
  means a planted marker can only add a root, never hide the real one.  A
  ``core.worktree`` in one of their git directories adds the tree it names.
* (a) A path is live when it, or any directory above it, is the same
  directory (device and file index, so junctions, symlinks and case
  differences do not matter) as a live root, whatever markers sit in between.
  Git skips a ``.git`` directory whose ``HEAD`` is not a ref and keeps walking
  up to the live repository; a valid nested repository is still inside the
  live tree.  Both the spelling given and the resolved path are walked.
* (b) A path is also live when a git directory reachable from it or from a
  directory above it (a ``.git`` directory or junction; a ``.git`` file's
  ``gitdir:``, read relative to the file's folder as git reads it; the
  directory itself when it holds a ``HEAD``) would let git act on live state
  from outside the live tree: it *is* a live git directory or the live
  common directory; its ``commondir`` is the live common directory and its
  ``HEAD`` follows (through loose symbolic refs) the branch a live checkout
  has checked out, so a commit there moves the live branch; its ``refs`` is
  the live ``refs`` without being a worktree of it, so git's "already checked
  out" check cannot see the live checkout; or its ``config`` /
  ``config.worktree`` sets ``core.worktree`` to a path inside a live root.
  Every ancestor is inspected instead of stopping where git stops, so this is
  a superset of the repository git can pick and does not depend on
  validating ``HEAD`` exactly as git does.

A linked worktree of the live repository (``git worktree add`` outside the
live tree; its ``.git`` file names ``<common>/worktrees/<name>``) is not live:
it has its own ``HEAD``, index and files, and git refuses to check out or
force-move a branch another worktree has checked out.  It does share refs
(branches, tags, the stash), which is why one whose ``HEAD`` was made to follow
the live branch is live, and why ``runtime_source_stash pop`` restores only a
stash it recorded itself (``git_tools``).

Not covered: a link planted deeper inside a git directory (``refs/heads`` as a
junction to the live one) needs a tool that creates links, which the file
tools do not; ``include`` directives are not followed, and git does not follow
them when it reads ``core.worktree`` during repository discovery either.

* :func:`is_live` applies (a) and (b).
* :func:`refusal` is the guard the tools call.

Nothing is cached; every call reads the disk as it is.  The functions hold no
state and may be called from any thread.  The cost is a bounded number of
``stat`` calls and reads of small files (at most 64 KiB each) per call, and no
git process.  A check and the git run that follows it are still two steps.
"""
from __future__ import annotations

import os
import stat
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.model_tool_policy as _model_tool_policy
import sonder_runtime.adapters.operator_attendance as _attendance
from sonder_runtime.platform import paths as runtime_paths

# Ancestors inspected above one path.
MAX_ANCESTORS = 64
_GIT_ENTRY = ".git"
_GITFILE_PREFIX = "gitdir:"
_SYMREF_PREFIX = "ref:"
# A pointer, HEAD or config file larger than this is not read (treated as absent).
_MAX_SMALL_FILE = 64 * 1024
# Loose symbolic refs followed from HEAD before giving up (git itself stops at 5).
_MAX_SYMREF_DEPTH = 5
_CONFIG_FILES = ("config", "config.worktree")


class LiveCheckoutGuard(str, Enum):
    """The ``guard`` field of a refused result."""

    LIVE_CHECKOUT = "live_checkout_attendance"


# ---- small readers ------------------------------------------------------------

def _stat(path: Path) -> os.stat_result | None:
    try:
        return os.stat(path)
    except (OSError, ValueError):
        return None


def _same(status: os.stat_result | None, others: Iterable[os.stat_result]) -> bool:
    return status is not None and any(os.path.samestat(status, other) for other in others)


def _ancestors(path: Path) -> Iterator[Path]:
    current = Path(path)
    for _ in range(MAX_ANCESTORS):
        yield current
        parent = current.parent
        if parent == current:
            return
        current = parent


def _read_small(path: Path) -> str | None:
    """The text of the regular file at ``path``, or None when it is missing, not regular or too large."""
    status = _stat(path)
    if status is None or not stat.S_ISREG(status.st_mode) or status.st_size > _MAX_SMALL_FILE:
        return None
    try:
        with open(path, "rb") as handle:
            data = handle.read(_MAX_SMALL_FILE)
    except (OSError, ValueError):
        return None
    return data.decode("utf-8", errors="surrogateescape")


def _first_line(text: str | None) -> str:
    return text.split("\n", 1)[0].rstrip("\r") if text else ""


def _named_paths(value: str, base: Path) -> list[Path]:
    """The paths a git pointer value can name: as written and trimmed, a relative one against ``base``."""
    named = []
    for text in dict.fromkeys((value, value.strip())):
        if text:
            candidate = Path(text)
            named.append(candidate if candidate.is_absolute() else base / candidate)
    return named


# ---- what git reads in a git directory ---------------------------------------------

def _gitfile_dirs(gitfile: Path) -> list[Path]:
    """The directories the ``gitdir:`` line of a ``.git`` file can name (relative to the file's folder)."""
    line = _first_line(_read_small(gitfile))
    if not line.startswith(_GITFILE_PREFIX):
        return []
    value = line[len(_GITFILE_PREFIX):]
    return _named_paths(value[1:] if value.startswith(" ") else value, gitfile.parent)


def _git_dirs_at(directory: Path) -> list[Path]:
    """The git directories a ``.git`` entry in ``directory`` can make git use; none without one."""
    entry = directory / _GIT_ENTRY
    status = _stat(entry)
    if status is None:
        return []
    if stat.S_ISDIR(status.st_mode):
        return [entry]
    if stat.S_ISREG(status.st_mode):
        return _gitfile_dirs(entry)
    return []


def _common_dirs(git_dir: Path) -> list[Path]:
    """The common directory of ``git_dir``: what its ``commondir`` file names (relative to it), else itself."""
    text = _read_small(git_dir / "commondir")
    if text is None:
        return [git_dir]
    return _named_paths(_first_line(text), git_dir) or [git_dir]


def _symref(text: str | None) -> str | None:
    line = _first_line(text)
    if not line.startswith(_SYMREF_PREFIX):
        return None
    name = line[len(_SYMREF_PREFIX):].strip()
    return name if name.startswith("refs/") else None


def _ref_key(name: str) -> str:
    # NTFS folds case: a HEAD naming refs/heads/MASTER writes the loose file refs/heads/master.
    return name.casefold() if os.name == "nt" else name


def _followed_branch(git_dir: Path, commons: list[Path]) -> str | None:
    """The ref a commit through ``git_dir``'s ``HEAD`` moves, through loose symbolic refs; None when detached."""
    name = _symref(_read_small(git_dir / "HEAD"))
    for _ in range(_MAX_SYMREF_DEPTH):
        if name is None:
            return None
        target = None
        for base in (git_dir, *commons):
            target = _symref(_read_small(base / name))
            if target is not None:
                break
        if target is None:
            return _ref_key(name)
        name = target
    return _ref_key(name) if name is not None else None


def _config_values(value: str) -> list[str]:
    """One config value as git unquotes it, and the plain trimmed text (either may be what git used)."""
    out: list[str] = []
    quoted = False
    escape = False
    for char in value.strip():
        if escape:
            out.append({"n": "\n", "t": "\t", "b": "\b"}.get(char, char))
            escape = False
        elif char == "\\":
            escape = True
        elif char == '"':
            quoted = not quoted
        elif not quoted and char in "#;":
            break
        else:
            out.append(char)
    return list(dict.fromkeys(item for item in ("".join(out).strip(), value.strip().strip('"')) if item))


def _core_worktree_values(text: str) -> list[str]:
    """Every ``core.worktree`` value in one config file's text (no includes: git does not follow them for it)."""
    section = ""
    values: list[str] = []
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("["):
            end = line.find("]")
            section = line[1:end].strip().casefold() if end > 0 else ""
            line = line[end + 1:].strip() if end > 0 else ""
        if section != "core" or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip().casefold() == "worktree":
            values.extend(_config_values(value))
    return values


def _configured_worktrees(git_dir: Path) -> list[Path]:
    """The working trees ``core.worktree`` names in ``git_dir``'s own config files (relative to it)."""
    found: list[Path] = []
    for name in _CONFIG_FILES:
        text = _read_small(git_dir / name)
        if text:
            for value in _core_worktree_values(text):
                found.extend(_named_paths(value, git_dir))
    return found


# ---- the live set ------------------------------------------------------------------

@dataclass(frozen=True)
class _Repository:
    """One live git directory: its identity, its common directory and ``refs``, and the branch it has checked out."""

    git_dir: os.stat_result
    commons: tuple[os.stat_result, ...]
    refs: tuple[os.stat_result, ...]
    branch: str | None


@dataclass(frozen=True)
class _Live:
    """The live roots and git directories, read once for one :func:`is_live` call."""

    roots: tuple[os.stat_result, ...]
    repositories: tuple[_Repository, ...]

    @property
    def git_dirs(self) -> tuple[os.stat_result, ...]:
        return tuple(item for repo in self.repositories for item in (repo.git_dir, *repo.commons))


def _control_locations() -> list[Path]:
    locations = [file_ops.workspace_root(), file_ops.RUNTIME_PACKAGE_DATA_DIR]
    try:
        locations.append(_model_tool_policy.overlay_path())
    except (OSError, ValueError, TypeError):
        pass
    try:
        locations.append(Path(runtime_paths.default_home()))
    except (OSError, ValueError, TypeError):
        pass
    return [file_ops._resolve_best_effort(Path(location)) for location in locations]


def _has_git_entry(directory: Path) -> bool:
    return os.path.lexists(directory / _GIT_ENTRY)


def _repository(git_dir: Path) -> _Repository | None:
    status = _stat(git_dir)
    if status is None or not stat.S_ISDIR(status.st_mode):
        return None
    commons = _common_dirs(git_dir)
    return _Repository(
        git_dir=status,
        commons=tuple(item for item in map(_stat, commons) if item is not None),
        refs=tuple(item for item in (_stat(common / "refs") for common in commons) if item is not None),
        branch=_followed_branch(git_dir, commons),
    )


def _live() -> _Live:
    roots: list[Path] = []
    for location in _control_locations():
        roots.extend(directory for directory in _ancestors(location) if _has_git_entry(directory))
    repositories: list[_Repository] = []
    trees: list[Path] = []
    for root in dict.fromkeys(roots):
        for git_dir in _git_dirs_at(root):
            repository = _repository(git_dir)
            if repository is not None:
                repositories.append(repository)
            trees.extend(_configured_worktrees(git_dir))
    root_status = tuple(item for item in map(_stat, [*roots, *trees]) if item is not None)
    return _Live(root_status, tuple(repositories))


def _spellings(path: Path) -> list[Path]:
    """``path`` as given (made absolute, links kept) and resolved."""
    return list(dict.fromkeys((Path(os.path.abspath(path)), file_ops._resolve_best_effort(Path(path)))))


def _inside_live_root(path: Path, live: _Live) -> bool:
    return any(
        _same(_stat(directory), live.roots)
        for spelled in _spellings(path) for directory in _ancestors(spelled)
    )


def _uses_live_state(git_dir: Path, live: _Live) -> bool:
    """Whether git using ``git_dir`` would act on a live checkout's ``HEAD``, index, branch or files."""
    status = _stat(git_dir)
    if status is None:
        return False
    if _same(status, live.git_dirs):
        return True
    commons = _common_dirs(git_dir)
    branch: str | None = None
    branch_read = False
    for common in commons:
        common_status = _stat(common)
        for repository in live.repositories:
            if _same(common_status, repository.commons):
                if not branch_read:
                    branch, branch_read = _followed_branch(git_dir, commons), True
                if branch is not None and branch == repository.branch:
                    return True
            elif _same(_stat(common / "refs"), repository.refs):
                return True
    return any(_inside_live_root(tree, live) for tree in _configured_worktrees(git_dir))


def _reaches_live(directory: Path, live: _Live) -> bool:
    """Whether a git directory found at ``directory`` (its ``.git``, or itself) would act on live state."""
    candidates = _git_dirs_at(directory)
    head = _stat(directory / "HEAD")
    if head is not None and stat.S_ISREG(head.st_mode):
        candidates.append(directory)
    return any(_uses_live_state(git_dir, live) for git_dir in candidates)


def is_live(path: Path) -> bool:
    """Whether git run at ``path`` could change the live runtime checkout (rules (a) and (b) above)."""
    live = _live()
    if not live.roots and not live.repositories:
        return False
    for spelled in _spellings(Path(path)):
        for directory in _ancestors(spelled):
            if _same(_stat(directory), live.roots) or _reaches_live(directory, live):
                return True
    return False


@dataclass(frozen=True)
class LiveCheckoutRefusal:
    """An unattended ``operation`` was refused in the live checkout."""

    operation: str

    @property
    def message(self) -> str:
        return (
            "refusing %s: the live Sonder runtime checkout is protected; it changes only when the "
            "operator types the command at the console (no developer token opens it)" % self.operation
        )

    def as_result(self) -> dict:
        """The dict shape the ``harness_tools`` functions return for a refusal."""
        return {"ok": False, "error": self.message, "guard": LiveCheckoutGuard.LIVE_CHECKOUT.value}


def refusal(operation: str, path: Path) -> LiveCheckoutRefusal | None:
    """The refusal for ``operation`` run in ``path``, or None when it may run.

    It may run when the operator typed ``operation`` at the console, or when
    ``path`` is not live (:func:`is_live`).  No subprocess runs.
    """
    if _attendance.attended_for(operation):
        return None
    return LiveCheckoutRefusal(operation) if is_live(path) else None


__all__ = ["LiveCheckoutGuard", "LiveCheckoutRefusal", "is_live", "refusal"]
