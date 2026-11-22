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

import fnmatch
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


def _dir_can_match(rel_parts: tuple[str, ...], pat_parts: tuple[str, ...]) -> bool:
    """Whether *pat_parts* can match *rel_parts* or any descendant beneath it."""
    if not rel_parts:
        return True
    if not pat_parts:
        return False
    if pat_parts[0] == "**":
        return True
    if fnmatch.fnmatch(rel_parts[0], pat_parts[0]):
        return _dir_can_match(rel_parts[1:], pat_parts[1:])
    return False


def _path_matches(rel_parts: tuple[str, ...], pat_parts: tuple[str, ...]) -> bool:
    """Match relative path segments against *pat_parts* (supporting ``**``)."""
    n_rel, n_pat = len(rel_parts), len(pat_parts)
    dp = [[False] * (n_pat + 1) for _ in range(n_rel + 1)]
    dp[n_rel][n_pat] = True
    for j in range(n_pat - 1, -1, -1):
        if pat_parts[j] == "**":
            dp[n_rel][j] = dp[n_rel][j + 1]
    for i in range(n_rel - 1, -1, -1):
        for j in range(n_pat - 1, -1, -1):
            if pat_parts[j] == "**":
                dp[i][j] = dp[i][j + 1] or dp[i + 1][j]
            elif fnmatch.fnmatch(rel_parts[i], pat_parts[j]):
                dp[i][j] = dp[i + 1][j + 1]
    return dp[0][0]


def _is_network_target(target: str) -> bool:
    """Whether a symlink/junction target spells a UNC, device, or NT path."""
    if target.strip().replace("/", "\\") == "\\??":
        return True
    try:
        file_ops._reject_network_path(target)
    except PermissionError:
        return True
    return False


def _vet_reparse_entry(entry: Path, rel: Path, real_root: str) -> None:
    """Refuse an escaping or network-targeted symlink/junction without touching SMB."""
    try:
        target = os.readlink(entry)
    except OSError as exc:
        raise ValueError(
            "glob match %s resolves outside the root (a link); narrow the glob" % rel
        ) from exc
    if _is_network_target(target):
        raise ValueError(
            "glob match %s resolves outside the root (a link); narrow the glob" % rel
        )
    if not _inside(real_root, os.path.normcase(os.path.realpath(entry))):
        raise ValueError(
            "glob match %s resolves outside the root (a link); narrow the glob" % rel
        )


def _refuse_secret_glob(root: Path, glob: str) -> None:
    """Refuse when *glob* under *root* matches a file ``text_search`` withholds,
    or a match that resolves outside *root* (pruning reparse-point dirs before descent)."""
    _refuse_escaping_glob(glob)
    pat_parts = tuple(part for part in _SEPARATORS.split(glob) if part)
    real_root = os.path.normcase(os.path.realpath(root))
    seen = 0
    for base, dirs, files in os.walk(root, followlinks=False):
        base_path = Path(base)
        next_dirs: list[str] = []
        for name in sorted(dirs):
            if name.startswith(".") and name not in (".", ".."):
                continue
            entry = base_path / name
            rel = entry.relative_to(root)
            if not _dir_can_match(rel.parts, pat_parts):
                continue
            seen += 1
            if seen > MAX_GLOB_ENTRIES:
                raise ValueError("glob matches too many entries to vet; narrow the glob")
            if file_ops._is_reparse_point(entry):
                _vet_reparse_entry(entry, rel, real_root)
                # Never descend into a symlink or Windows junction directory
                # (an in-root link could alias a dot-dir like .ssh or .aws).
                raise ValueError(
                    "glob match %s traverses a link; narrow the glob" % rel
                )
            next_dirs.append(name)
        dirs[:] = next_dirs
        for name in sorted(files):
            if name.startswith(".") and name not in (".", ".."):
                continue
            entry = base_path / name
            rel = entry.relative_to(root)
            if not _path_matches(rel.parts, pat_parts):
                continue
            seen += 1
            if seen > MAX_GLOB_ENTRIES:
                raise ValueError("glob matches too many entries to vet; narrow the glob")
            if file_ops._is_reparse_point(entry):
                _vet_reparse_entry(entry, rel, real_root)
            elif not _inside(real_root, os.path.normcase(os.path.realpath(entry))):
                raise ValueError(
                    "glob match %s resolves outside the root (a link); narrow the glob" % rel
                )
            if not entry.is_file():
                continue
            if workbench._secret_file(file_ops._resolve_best_effort(entry)):
                raise ValueError(
                    "glob matches a secret or credential file (%s); narrow the glob" % rel
                )


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
        real_root = os.path.normcase(os.path.realpath(resolved_root))
        for side in ("left", "right"):
            value = str(args.get(side) or "").strip()
            if not value:
                continue
            target = Path(value).expanduser()
            if not target.is_absolute():
                target = resolved_root / target
            resolved_side = file_ops.resolve_repository_read_path(
                str(target), allow_workspace_root=False,
                reject_sensitive=True, extra_roots=extra_roots,
            )
            if not _inside(real_root, os.path.normcase(os.path.realpath(resolved_side))):
                raise ValueError(
                    "diff_files %s path must stay inside the root" % side
                )
    elif tool_name == "find_references":
        _refuse_secret_glob(resolved_root, str(args.get("glob") or "**/*.py"))
