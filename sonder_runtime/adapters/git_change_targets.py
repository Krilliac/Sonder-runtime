"""Refuse an ``apply_patch`` that would write a protected control-plane path.

``apply_patch`` is a file-writing tool like ``file_write``, so every path the
patch writes gets the file tools' protected-path guard, in any repository.
``git apply --numstat -z`` parses the patch exactly as the apply will (a rename
names both paths) and lists them before anything is written; a patch git cannot
list is refused.

The git tools that rewrite a working tree (checkout, branch with a base, merge,
cherry-pick, stash) do not predict their writes: in the live runtime checkout
they do not run unattended at all (:mod:`live_checkout`), and elsewhere they
behave as plain git.

Each path is checked under the working root and under the repository's top
level (git resolves patch paths against one or the other depending on where it
runs), so neither spelling escapes.  The protected places are read once per
call (the exact control-plane files, the policy files, the package data
directory) rather than once per path.  A refusal names the repository-relative
path only, never the host path.

``run`` is the caller's git runner: ``run(arguments, stdin_bytes) ->
(returncode, stdout_bytes)``.  Nothing is cached; the functions hold no state
and may be called from any thread.
"""
from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path

import sonder_runtime.adapters.filesystem.file_ops as file_ops
import sonder_runtime.adapters.filesystem.operator_only_paths as _operator_only

Runner = Callable[[Sequence[str], bytes], tuple[int, bytes]]


class TargetsUnknown(Exception):
    """git could not say which paths an operation would write."""


def toplevel(run: Runner) -> Path | None:
    """The repository's top level, or None outside a repository."""
    try:
        code, output = run(["rev-parse", "--show-toplevel"], b"")
    except Exception:  # noqa: BLE001 - not a repository
        return None
    text = output.decode("utf-8", "surrogateescape").strip()
    return Path(text) if code == 0 and text else None


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


# ---- the guard ----------------------------------------------------------------------

def _message(operation: str, relative: str) -> str:
    return (
        "refusing %s: it would change the protected Sonder control-plane path %s "
        "(refused without an authenticated developer token)" % (operation, relative.replace(os.sep, "/"))
    )


def patch_refusal(operation: str, root: Path, top: Path | None, paths: Iterable[str]) -> str:
    """Why ``operation`` may not write the repository-relative ``paths``, or ``""``.

    ``top`` is the repository's top level (:func:`toplevel`), or None outside
    a repository.  A path is refused when it is, under any spelling or name,
    an exact control-plane file or a policy file, lies in the package data
    directory, has a git metadata component, or has no ordinary spelling.
    """
    exact = set(file_ops._control_plane_paths())
    policy = _operator_only.locate(file_ops.RUNTIME_PACKAGE_DATA_DIR, file_ops._resolve_best_effort)
    exact.update(policy.exact)
    data_dir = file_ops._resolve_best_effort(file_ops.RUNTIME_PACKAGE_DATA_DIR)
    bases = [Path(root)] + ([Path(top)] if top is not None else [])
    for relative in dict.fromkeys(paths):
        for base in bases:
            candidate = base / relative
            try:
                spelled = file_ops._protection_spelling(candidate)
            except (OSError, ValueError):
                return _message(operation, relative)
            if (
                spelled in exact
                or file_ops._is_inside(spelled, data_dir)
                or _operator_only.is_policy_file(spelled, policy)
                or file_ops._git_metadata_component(spelled)
                or file_ops._is_protected_mutation_path(candidate)
                or file_ops._is_interpreter_environment_path(spelled)
                or bool(file_ops._sensitive_mutation_component(candidate))
            ):
                return _message(operation, relative)
    return ""


def unlisted_refusal(operation: str, exc: TargetsUnknown) -> str:
    """The refusal for a patch whose paths git could not list."""
    return (
        "refusing %s: the paths it would change could not be listed (%s), so it cannot be "
        "checked against the protected Sonder control-plane paths" % (operation, exc)
    )


__all__ = ["Runner", "TargetsUnknown", "patch_refusal", "patch_targets", "toplevel", "unlisted_refusal"]
