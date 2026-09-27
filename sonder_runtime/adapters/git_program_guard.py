"""Keep repository git configuration from launching host programs.

Whoever can write a repository's ``.git/config`` (a model with file tools, or
the author of a cloned checkout) chooses programs git runs as a side effect of
ordinary commands: a hook, ``core.fsmonitor`` (run by ``status`` and ``diff``
on every index refresh), a filter ``clean``/``smudge``/``process`` program
(run when a stat-dirty file is re-hashed or a file is checked out), a merge
driver, a diff ``textconv`` or a signing program.  Both the read-only git
tools (``git_tools``) and the harness mutation tools (``harness_tools``) must
run git with each of those replaced by a fixed built-in equivalent.

This module owns that override list.  Callers supply a way to run one
``git config`` probe (the only git command that is run without the
overrides; it launches no program) and receive ``-c`` arguments to place
before the subcommand.  When the driver configuration cannot be read the
helpers raise :class:`GitProgramConfigError`, and callers refuse to run git
rather than run it with a driver left in place.

External diff drivers (``diff.external`` and ``diff.<driver>.command``) have
no built-in replacement value; porcelain ``diff``/``log``/``show`` callers
pass ``--no-ext-diff`` (and ``--no-textconv``) instead, and no other
subcommand runs them.
"""
from __future__ import annotations

import os
import re
from typing import Callable, Iterable, Mapping

# Repository config keys that name a program git runs.
GIT_PROGRAM_KEYS = (
    r"^(filter\..*\.(clean|smudge|process|required)"
    r"|merge\..*\.driver|diff\..*\.textconv)$"
)
_GIT_PROGRAM_KEY_RE = re.compile(
    r"^(?P<section>filter|merge|diff)\.(?P<name>.+)\."
    r"(?P<key>clean|smudge|process|required|driver|textconv)$",
    re.IGNORECASE,
)
# ``git merge-file`` is git's own three-way text merge: what the built-in
# driver does, with none of the repository's configuration.
BUILTIN_MERGE_DRIVER = "git merge-file --marker-size=%L %A %O %B"

# Arguments (after ``git``) of the one probe that reads driver configuration.
CONFIG_PROBE_ARGUMENTS = (
    "config", "--null", "--name-only", "--get-regexp", GIT_PROGRAM_KEYS,
)


class GitProgramConfigError(ValueError):
    """Driver configuration could not be read or had an unexpected shape."""


def static_overrides() -> list[str]:
    """Overrides that apply whatever the repository configures."""
    return [
        "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false",
        "-c", "core.sshCommand=",
        "-c", "commit.gpgSign=false",
        "-c", "tag.gpgSign=false",
        "-c", "tag.forceSignAnnotated=false",
    ]


def driver_overrides(keys: Iterable[str]) -> list[str]:
    """Built-in replacements for every configured filter/merge/diff driver."""
    arguments: list[str] = []
    seen = set()
    for key in keys:
        key = str(key).strip()
        if not key:
            continue
        match = _GIT_PROGRAM_KEY_RE.fullmatch(key)
        if match is None:
            raise GitProgramConfigError("unexpected git driver configuration key: %s" % key)
        section, name = match.group("section").lower(), match.group("name")
        if (section, name) in seen:
            continue
        seen.add((section, name))
        prefix = "%s.%s." % (section, name)
        if section == "filter":
            # ``process=`` disables the long-running protocol; ``cat`` is a
            # passthrough for the clean and smudge directions.
            arguments.extend([
                "-c", prefix + "process=",
                "-c", prefix + "clean=cat",
                "-c", prefix + "smudge=cat",
                "-c", prefix + "required=false",
            ])
        elif section == "merge":
            arguments.extend(["-c", prefix + "driver=" + BUILTIN_MERGE_DRIVER])
        else:
            arguments.extend(["-c", prefix + "textconv=cat"])
    return arguments


def neutralized_git_arguments(
    run_probe: Callable[[list[str]], Mapping[str, object]],
) -> list[str]:
    """Return every ``-c`` override for the repository ``run_probe`` targets.

    ``run_probe`` receives :data:`CONFIG_PROBE_ARGUMENTS` (arguments after
    the git executable) and returns a mapping with ``returncode``, ``stdout``
    (text), ``stderr`` and optionally ``timed_out``/``truncated``.  Exit 1 is
    git's "no matching key".  Anything else raises
    :class:`GitProgramConfigError` so the caller fails closed.
    """
    probe = run_probe(list(CONFIG_PROBE_ARGUMENTS))
    if probe.get("timed_out"):
        raise GitProgramConfigError("git driver configuration probe timed out")
    if probe.get("returncode") not in (0, 1):
        detail = str(probe.get("stderr") or probe.get("stdout") or "").strip()
        raise GitProgramConfigError(
            "git driver configuration could not be read: %s" % (detail or "no diagnostic")
        )
    if probe.get("truncated"):
        raise GitProgramConfigError("git driver configuration is too large to neutralize safely")
    stdout = str(probe.get("stdout") or "")
    return static_overrides() + driver_overrides(stdout.split("\x00"))
