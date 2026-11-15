"""Refuse a patch or a git operation that would change protected control-plane files.

The general file tools refuse protected paths one name at a time
(``file_ops._require_mutation_access``).  ``apply_patch`` and the git tools
that change the working tree write files git chooses, so the same guard is
applied here to every path the operation would write, listed by git itself
before the operation runs:

* ``apply_patch``: ``git apply --numstat -z`` parses the patch exactly as the
  apply will (a rename names both paths).  Every listed path is checked, in
  any repository, because ``apply_patch`` is a file-writing tool like
  ``file_write``.  A patch git cannot list is refused.
* checkout of a branch or commit, ``git_branch`` with a base, merge,
  cherry-pick, stash push/pop/apply: the paths that differ between ``HEAD``
  and what the operation brings in (``git diff --name-only``), the index
  paths a checkout of a pathspec restores, and for a stash push the modified
  and (with ``-u``) untracked paths it would revert or remove.  These are
  checked only in the live runtime checkout: a repository whose top level
  holds a control-plane location (the workspace root, the package data
  directory, the operator overlay or the Sonder home).  Elsewhere the git
  tools keep their behaviour, so a user's repository whose tracked test
  certificate changes between branches still switches.  In the live checkout
  a list git cannot produce refuses the operation.

Each path is checked both under the working root and under the repository's
top level (git resolves patch paths against one or the other depending on
where it runs), so neither spelling escapes.  A refusal names the
repository-relative path only, never the host path.

``run`` is the caller's git runner: ``run(arguments, stdin_bytes) ->
(returncode, stdout_bytes)``, running git in the working root with whatever
hardening the caller applies.  Nothing here is cached; the functions hold no
state and may be called from any thread.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.model_tool_policy as _model_tool_policy
from sonder_runtime.platform import paths as runtime_paths

Runner = Callable[[Sequence[str], bytes], tuple[int, bytes]]
_NAME_ONLY = ("--name-only", "-z", "--no-renames", "--no-ext-diff", "--no-textconv")


class TargetsUnknown(Exception):
    """git could not say which paths an operation would write."""


def _split(output: bytes) -> list[str]:
    return [item for item in output.decode("utf-8", "surrogateescape").split("\0") if item]


def _listed(run: Runner, arguments: Sequence[str], stdin: bytes = b"") -> list[str]:
    try:
        code, output = run(list(arguments), stdin)
    except Exception as exc:  # noqa: BLE001 - any failure to list refuses
        raise TargetsUnknown("git %s failed (%s)" % (arguments[0], type(exc).__name__)) from exc
    if code != 0:
        raise TargetsUnknown("git %s exited %s" % (arguments[0], code))
    return _split(output)


def _revision(run: Runner, ref: str) -> str:
    """The commit ``ref`` names, or ``""`` when it names none."""
    if not ref or ref.startswith("-"):
        return ""
    try:
        # ``ref`` never starts with "-" here, so it cannot be read as an option.
        code, output = run(["rev-parse", "--verify", "--quiet", ref + "^{commit}"], b"")
    except Exception:  # noqa: BLE001 - not a revision
        return ""
    return output.decode("utf-8", "replace").strip() if code == 0 else ""


def _require_revision(run: Runner, ref: str) -> str:
    commit = _revision(run, ref)
    if not commit:
        raise TargetsUnknown("%r does not name a commit" % (ref,))
    return commit


def toplevel(run: Runner) -> Path | None:
    """The repository's top level, or None outside a repository."""
    try:
        code, output = run(["rev-parse", "--show-toplevel"], b"")
    except Exception:  # noqa: BLE001 - not a repository
        return None
    text = output.decode("utf-8", "surrogateescape").strip()
    return Path(text) if code == 0 and text else None


# ---- what each operation writes -------------------------------------------------

def patch_targets(run: Runner, patch_text: str) -> list[str]:
    """Every path ``git apply`` would write for ``patch_text`` (both names of a rename)."""
    records = _listed_raw(run, ["apply", "--numstat", "-z", "-"], patch_text.encode("utf-8"))
    paths: list[str] = []
    index = 0
    while index < len(records):
        fields = records[index].split("\t", 2)
        if len(fields) != 3:
            raise TargetsUnknown("git apply --numstat printed an unexpected record")
        if fields[2]:
            paths.append(fields[2])
            index += 1
        else:  # a rename or copy: the two names follow
            if index + 2 >= len(records):
                raise TargetsUnknown("git apply --numstat printed an incomplete rename")
            paths.extend(records[index + 1:index + 3])
            index += 3
    return paths


def _listed_raw(run: Runner, arguments: Sequence[str], stdin: bytes) -> list[str]:
    try:
        code, output = run(list(arguments), stdin)
    except Exception as exc:  # noqa: BLE001 - any failure to list refuses
        raise TargetsUnknown("git %s failed (%s)" % (arguments[0], type(exc).__name__)) from exc
    if code != 0:
        raise TargetsUnknown("git %s exited %s" % (arguments[0], code))
    text = output.decode("utf-8", "surrogateescape")
    records = text.split("\0")
    if records and records[-1] == "":
        records.pop()
    return records


def checkout_targets(run: Runner, ref: str) -> list[str]:
    """What ``git checkout <ref>`` writes: the tree difference for a commit, else the pathspec's index paths."""
    commit = _revision(run, ref)
    if commit:
        return _listed(run, ["diff", *_NAME_ONLY, "HEAD", commit, "--"])
    if not ref or ref.startswith("-"):
        raise TargetsUnknown("%r is neither a commit nor a path" % (ref,))
    return _listed(run, ["ls-files", "-z", "--", ref])


def merge_targets(run: Runner, branch: str) -> list[str]:
    """What merging ``branch`` writes: its changes since the merge base."""
    commit = _require_revision(run, branch)
    return _listed(run, ["diff", *_NAME_ONLY, "HEAD..." + commit, "--"])


def cherry_pick_targets(run: Runner, commits: Iterable[str]) -> list[str]:
    """What cherry-picking ``commits`` writes: each one's own changes."""
    paths: list[str] = []
    for ref in commits:
        commit = _require_revision(run, str(ref))
        paths.extend(_listed(run, ["diff-tree", "-r", "--root", "-m", "--no-commit-id", *_NAME_ONLY, commit]))
    return paths


def stash_targets(run: Runner, action: str, *, include_untracked: bool) -> list[str]:
    """What a stash action writes: push reverts (and removes) local changes, pop/apply restores the stash."""
    if action == "push":
        paths = _listed(run, ["diff", *_NAME_ONLY, "HEAD", "--"])
        if include_untracked:
            paths += _listed(run, ["ls-files", "-z", "--others", "--exclude-standard"])
        return paths
    if action in ("pop", "apply"):
        return _listed(run, ["stash", "show", "--include-untracked", *_NAME_ONLY, "stash@{0}"])
    return []


# ---- the guard ----------------------------------------------------------------------

def _control_plane_locations() -> list[Path]:
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


def is_live_checkout(top: Path | None) -> bool:
    """Whether the repository at ``top`` holds a control-plane location."""
    if top is None:
        return False
    top = file_ops._resolve_best_effort(top)
    return any(location == top or file_ops._is_inside(location, top) for location in _control_plane_locations())


def protected_target(root: Path, top: Path | None, paths: Iterable[str]) -> str:
    """The first repository-relative path in ``paths`` that is protected, or ``""``."""
    bases = [Path(root)] + ([Path(top)] if top is not None else [])
    for relative in dict.fromkeys(paths):
        for base in bases:
            try:
                file_ops._require_mutation_access(base / relative, False)
            except PermissionError:
                return relative
    return ""


def refusal(
    operation: str, root: Path, run: Runner, targets: Callable[[Runner], list[str]], *, top: Path | None,
) -> str:
    """Why ``operation`` may not run, or ``""``.

    ``targets`` lists the repository-relative paths it would write (with
    ``run``); ``top`` is the repository's top level (:func:`toplevel`), or
    None outside a repository.  The caller decides whether the check applies
    (:func:`is_live_checkout` for the git tools; always for a patch).
    """
    try:
        paths = targets(run)
    except TargetsUnknown as exc:
        return (
            "refusing %s: the paths it would change could not be listed (%s), so it cannot be "
            "checked against the protected Sonder control-plane paths" % (operation, exc)
        )
    blocked = protected_target(Path(root), top, paths)
    if blocked:
        return (
            "refusing %s: it would change the protected Sonder control-plane path %s "
            "(refused without an authenticated developer token)" % (operation, blocked.replace(os.sep, "/"))
        )
    return ""


__all__ = [
    "Runner", "TargetsUnknown", "checkout_targets", "cherry_pick_targets", "is_live_checkout",
    "merge_targets", "patch_targets", "protected_target", "refusal", "stash_targets", "toplevel",
]
