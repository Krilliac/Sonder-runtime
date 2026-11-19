"""Read-only gate for the root-taking inspection tools.

``find_references`` and ``diff_files`` take a ``root`` (and ``diff_files``
also ``left`` and ``right``, relative to it). ``secret_scan`` is not one of
them: its walk follows in-root links to files outside the root, which this
gate does not vet, so the read-only lane refuses it. The read-only
agent layer calls :func:`check_root_confined_read` before dispatch so those
paths are confined and sensitive-path-checked, and a reference search whose
glob would read a secret or credential file, or a file outside the root, is
refused: ``text_search`` withholds those files, so a tool that reads the same
files must not hand them back.

The gate re-walks the glob the reader (``harness_tools._refactor_source_files``)
will walk, and refuses wherever its view and the reader's could differ: a glob
that is absolute or climbs with ``..``, a match whose real path leaves the root
(a symlinked file or directory), and a relative ``root`` that the reader would
resolve against a different directory (the process working directory) from the
one this gate vetted (the workspace).

Thread affinity: none (pure function over the filesystem). Raises
``ValueError``/``OSError``/``PermissionError`` for a refusal, which the
caller turns into its ``ERROR:`` text. A refusal names the glob match relative
to the root, never the outside target it resolves to.
"""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from pathlib import Path, PurePosixPath, PureWindowsPath

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.workbench as workbench

ROOT_CONFINED_READ_TOOLS = frozenset({"find_references", "diff_files"})

# Entries the glob walk may examine before the gate refuses the glob as too
# broad to vet: a walk that stops checking must not pass what it did not check.
MAX_GLOB_ENTRIES = 20_000

_SEPARATORS = re.compile(r"[\\/]+")


def _refuse_escaping_glob(glob: str) -> None:
    """Refuse a glob that is empty, absolute, anchored or climbs with ``..``."""
    parts = [part for part in _SEPARATORS.split(glob) if part]
    if (not parts or PurePosixPath(glob).is_absolute() or PureWindowsPath(glob).anchor
            or ".." in parts):
        raise ValueError("glob must be a relative pattern inside the root")


def _inside(real_root: str, real_path: str) -> bool:
    try:
        return os.path.commonpath([real_root, real_path]) == real_root
    except ValueError:  # different drives, or a mix of absolute and relative
        return False


def _refuse_secret_glob(root: Path, glob: str) -> None:
    """Refuse when *glob* under *root* matches a file ``text_search`` withholds,
    or a match that resolves outside *root*."""
    _refuse_escaping_glob(glob)
    real_root = os.path.normcase(os.path.realpath(root))
    seen = 0
    try:
        for path in root.glob(glob):
            seen += 1
            if seen > MAX_GLOB_ENTRIES:
                raise ValueError("glob matches too many entries to vet; narrow the glob")
            try:
                rel = path.relative_to(root)
            except ValueError as exc:
                raise ValueError("glob matches an entry outside the root") from exc
            if ".." in rel.parts:
                raise ValueError("glob matches an entry outside the root")
            # extract_references skips dot-paths below the root, so those are
            # never read; every other match is judged on what the OS resolves.
            if any(part.startswith(".") and part not in (".", "..") for part in rel.parts):
                continue
            if not _inside(real_root, os.path.normcase(os.path.realpath(path))):
                raise ValueError(
                    "glob match %s resolves outside the root (a link); narrow the glob" % rel
                )
            if not path.is_file():
                continue
            if workbench._secret_file(file_ops._resolve_best_effort(path)):
                raise ValueError(
                    "glob matches a secret or credential file (%s); narrow the glob" % rel
                )
    except NotImplementedError as exc:
        raise ValueError("glob is not a relative pattern: %s" % exc) from exc


def _refuse_relative_root_mismatch(root: str, resolved_root: Path) -> None:
    """Refuse a relative *root* the reader would resolve somewhere else.

    The reader resolves a relative root against the process working directory;
    :func:`file_ops.resolve_repository_read_path` resolves it against the
    workspace. When the two differ, the gate would vet one tree and the tool
    read another.
    """
    if Path(root).expanduser().is_absolute():
        return
    reader_root = os.path.normcase(os.path.realpath(Path(root).resolve()))
    if reader_root != os.path.normcase(os.path.realpath(resolved_root)):
        raise ValueError(
            "relative root does not name the same directory for the tool as for "
            "this check (the working directory is not the workspace); pass an "
            "absolute root"
        )


def check_root_confined_read(
    tool_name: str, args: Mapping[str, object], extra_roots: str = "",
) -> None:
    """Confine and vet the paths a root-taking read tool is about to read."""
    root = str(args.get("root", "") or ".")
    resolved_root = file_ops.resolve_repository_read_path(
        root, allow_workspace_root=True, reject_sensitive=True, extra_roots=extra_roots,
    )
    _refuse_relative_root_mismatch(root, resolved_root)
    if tool_name == "diff_files":
        for side in ("left", "right"):
            value = str(args.get(side) or "").strip()
            if not value:
                continue
            target = Path(value).expanduser()
            if not target.is_absolute():
                target = resolved_root / target
            file_ops.resolve_repository_read_path(
                str(target), allow_workspace_root=False,
                reject_sensitive=True, extra_roots=extra_roots,
            )
    elif tool_name == "find_references":
        _refuse_secret_glob(resolved_root, str(args.get("glob") or "**/*.py"))
