"""Ask git itself which repository it would use: the live-checkout rule's second check.

:mod:`live_checkout` decides from the files git reads.  This module runs
``git rev-parse`` from the target directory and reports git's own answer, so
that a gap in that file rule (a parse that differs from git's, a git version
that reads one more file) is caught when git still names the live
repository.  The caller ORs the two: an answer here can only add a refusal.

* The binary is the one the git tools run, ``shutil.which("git") or "git"``
  (``harness_tools._git``).  When that search found nothing and the bare
  name cannot be started either, the tools cannot run git, so the answer is
  :attr:`ProbeOutcome.NO_GIT`.
* Every ``GIT_*`` variable is removed (the tools remove the ones that move
  the repository; removing the rest only makes git look further), the
  locale is ``C`` so git's messages are the English ones matched here, and
  stdin is closed.
* ``rev-parse --path-format=absolute --git-dir --git-common-dir`` names the
  git directories; ``--show-toplevel`` runs separately because it fails in
  a bare repository.  Only git's exact "not a git repository" message counts
  as no repository; any other failure, a timeout, or output this module
  cannot parse is :attr:`ProbeOutcome.UNKNOWN`.

Each call starts two short git processes (one when there is no repository)
and reads nothing else.  ``rev-parse`` runs no hook, filter or fsmonitor.  The
module holds no state and may be called from any thread; the probe and the
tool's own git run are still two steps.
"""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import sonder_runtime.adapters.git_metadata_reads as reads

PROBE_TIMEOUT_SECONDS = 10.0
_GIT_DIRS_ARGUMENTS = ("rev-parse", "--path-format=absolute", "--git-dir", "--git-common-dir")
_TOPLEVEL_ARGUMENTS = ("rev-parse", "--path-format=absolute", "--show-toplevel")
_NOT_A_REPOSITORY = "fatal: not a git repository (or any of the parent directories): .git"
_NOT_A_REPOSITORY_AT_MOUNT = "fatal: not a git repository (or any parent up to mount point "
_NO_WORK_TREE = "fatal: this operation must be run in a work tree"
_LOCALE_VARIABLES = ("LANG", "LANGUAGE")


class ProbeOutcome(Enum):
    NO_GIT = "no git executable"
    NOT_A_REPOSITORY = "not a git repository"
    FOUND = "a repository"
    UNKNOWN = "unknown"


class ProbeUnknown(str, Enum):
    """Why git's answer could not be known."""

    NO_DIRECTORY = "no existing directory to run git from"
    SPAWN_FAILED = "git could not be started"
    TIMED_OUT = "git did not answer in time"
    FAILED = "git failed with an unexpected message"
    UNPARSED_OUTPUT = "git's output could not be parsed"


@dataclass(frozen=True)
class GitLocation:
    """What git reported, all absolute: its git directory, common directory and work tree (None when bare)."""

    git_dir: Path
    common_dir: Path
    toplevel: Path | None


@dataclass(frozen=True)
class ProbeAnswer:
    outcome: ProbeOutcome
    location: GitLocation | None = None
    unknown: ProbeUnknown | None = None


def _environment() -> dict[str, str]:
    env = {
        name: value for name, value in os.environ.items()
        if not name.upper().startswith(("GIT_", "LC_")) and name.upper() not in _LOCALE_VARIABLES
    }
    env.update({"LC_ALL": "C", "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0"})
    return env


def _start_directory(path: Path) -> Path | None:
    """The nearest existing directory at or above ``path`` (git runs there); None when it cannot be known."""
    current = Path(os.path.abspath(path))
    while True:
        status = reads.status_of(current)
        if status.state is reads.ReadState.UNKNOWN:
            return None
        if status.state is reads.ReadState.OK and stat.S_ISDIR(status.result.st_mode):
            return current
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _absolute_lines(stdout: bytes, count: int) -> list[Path] | None:
    """Exactly ``count`` absolute paths, one per line, or None."""
    try:
        text = stdout.decode("utf-8")
    except UnicodeDecodeError:
        return None
    lines = text.split("\n")
    if len(lines) != count + 1 or lines[-1] != "":
        return None
    paths = []
    for line in lines[:-1]:
        if os.name == "nt":
            line = line.removesuffix("\r")
        candidate = Path(line)
        if not line or reads.path_text_problem(line) is not None or not candidate.is_absolute():
            return None
        paths.append(candidate)
    return paths


def _first_error_line(stderr: bytes) -> str:
    return stderr.decode("utf-8", errors="replace").split("\n", 1)[0].rstrip("\r")


def _run(git: str, arguments: tuple[str, ...], cwd: Path, *, searched: bool) -> subprocess.CompletedProcess | ProbeAnswer:
    """One git run; an answer instead when it could not run.

    ``searched`` is True when ``git`` is the bare name (``shutil.which``
    found nothing): the tools start that same name, so when it cannot be
    found git cannot run for them either.
    """
    try:
        return subprocess.run(
            [git, *arguments], cwd=str(cwd), env=_environment(), stdin=subprocess.DEVNULL,
            capture_output=True, timeout=PROBE_TIMEOUT_SECONDS, check=False,
        )
    except subprocess.TimeoutExpired:
        return ProbeAnswer(ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.TIMED_OUT)
    except FileNotFoundError:
        if searched:
            return ProbeAnswer(ProbeOutcome.NO_GIT)
        return ProbeAnswer(ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.SPAWN_FAILED)
    except (OSError, ValueError):
        return ProbeAnswer(ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.SPAWN_FAILED)


def probe(path: Path) -> ProbeAnswer:
    """Which repository git, run at ``path`` (or the nearest directory above it), would use."""
    located = shutil.which("git")
    git = located or "git"
    start = _start_directory(path)
    if start is None:
        return ProbeAnswer(ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.NO_DIRECTORY)
    dirs = _run(git, _GIT_DIRS_ARGUMENTS, start, searched=located is None)
    if isinstance(dirs, ProbeAnswer):
        return dirs
    if dirs.returncode != 0:
        message = _first_error_line(dirs.stderr)
        if not dirs.stdout and (message == _NOT_A_REPOSITORY or message.startswith(_NOT_A_REPOSITORY_AT_MOUNT)):
            return ProbeAnswer(ProbeOutcome.NOT_A_REPOSITORY)
        return ProbeAnswer(ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.FAILED)
    named = _absolute_lines(dirs.stdout, 2)
    if named is None:
        return ProbeAnswer(ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.UNPARSED_OUTPUT)
    top = _run(git, _TOPLEVEL_ARGUMENTS, start, searched=located is None)
    if isinstance(top, ProbeAnswer):
        return top if top.outcome is ProbeOutcome.UNKNOWN else ProbeAnswer(
            ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.SPAWN_FAILED,
        )
    toplevel: Path | None = None
    if top.returncode == 0:
        lines = _absolute_lines(top.stdout, 1)
        if lines is None:
            return ProbeAnswer(ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.UNPARSED_OUTPUT)
        toplevel = lines[0]
    elif _first_error_line(top.stderr) != _NO_WORK_TREE:
        return ProbeAnswer(ProbeOutcome.UNKNOWN, unknown=ProbeUnknown.FAILED)
    return ProbeAnswer(ProbeOutcome.FOUND, GitLocation(named[0], named[1], toplevel))


__all__ = ["GitLocation", "PROBE_TIMEOUT_SECONDS", "ProbeAnswer", "ProbeOutcome", "ProbeUnknown", "probe"]
