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
    """Overrides that apply whatever the repository configures.

    Command-line ``-c`` values reach child git processes that run inside
    submodules (through ``GIT_CONFIG_PARAMETERS``), so these also hold there.
    The submodule settings stop ``status``/``diff``/``checkout``/``merge``/
    ``fetch`` from descending into submodules at all: a child git in a
    submodule reads the SUBMODULE's own config, whose drivers the
    superproject probe never sees.  ``git add`` still inspects populated
    submodules; callers that run it also apply
    :func:`submodule_driver_overrides`.
    """
    return [
        "-c", "core.hooksPath=" + os.devnull,
        "-c", "core.fsmonitor=false",
        "-c", "core.sshCommand=",
        "-c", "commit.gpgSign=false",
        "-c", "tag.gpgSign=false",
        "-c", "tag.forceSignAnnotated=false",
        "-c", "submodule.recurse=false",
        "-c", "diff.ignoreSubmodules=all",
        "-c", "diff.submodule=short",
        "-c", "status.submoduleSummary=false",
        "-c", "fetch.recurseSubmodules=false",
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


# Arguments (after ``git`` and :func:`static_overrides`) that list index
# entries; gitlinks (mode 160000) are the submodules git may descend into.
GITLINK_PROBE_ARGUMENTS = ("ls-files", "-z", "--stage")
MAX_SUBMODULES = 64
MAX_SUBMODULE_DEPTH = 8


def gitlink_paths(listing: str) -> list[str]:
    """Paths of gitlink entries in ``git ls-files -z --stage`` output."""
    paths = []
    for record in listing.split("\x00"):
        meta, tab, path = record.partition("\t")
        if tab and meta.split(" ", 1)[0] == "160000":
            paths.append(path)
    return paths


def _checked_probe(probe, what: str, *, accept=(0,)) -> str:
    if probe.get("timed_out"):
        raise GitProgramConfigError("git %s probe timed out" % what)
    if probe.get("returncode") not in accept:
        detail = str(probe.get("stderr") or probe.get("stdout") or "").strip()
        raise GitProgramConfigError(
            "git %s could not be read: %s" % (what, detail or "no diagnostic")
        )
    if probe.get("truncated"):
        raise GitProgramConfigError("git %s is too large to neutralize safely" % what)
    return str(probe.get("stdout") or "")


def submodule_driver_overrides(
    run_probe_at: Callable[[str, list[str]], Mapping[str, object] | None],
) -> list[str]:
    """Built-in replacements for drivers configured inside populated submodules.

    ``git add`` runs ``git status`` inside every populated submodule whatever
    the ignore settings say, and that child reads the submodule's own
    ``.git/config``.  Command-line overrides propagate into the child, so
    neutralizing the submodules' driver names here covers it.

    ``run_probe_at(relative_path, arguments)`` runs git in the repository
    checked out at ``relative_path`` (``""`` is the root repository) and
    returns the same mapping as ``neutralized_git_arguments``'s probe, or
    ``None`` when no repository is checked out there.  Nested submodules are
    followed; more than :data:`MAX_SUBMODULES` or deeper than
    :data:`MAX_SUBMODULE_DEPTH` raises :class:`GitProgramConfigError`.
    """
    keys: list[str] = []
    pending = [("", 0)]
    visited = 0
    while pending:
        relative, depth = pending.pop()
        listing = run_probe_at(relative, [*static_overrides(), *GITLINK_PROBE_ARGUMENTS])
        if listing is None:
            continue
        for path in gitlink_paths(_checked_probe(listing, "submodule list")):
            child = "%s/%s" % (relative, path) if relative else path
            visited += 1
            if visited > MAX_SUBMODULES or depth + 1 > MAX_SUBMODULE_DEPTH:
                raise GitProgramConfigError("too many nested submodules to neutralize safely")
            probe = run_probe_at(child, list(CONFIG_PROBE_ARGUMENTS))
            if probe is None:
                continue
            keys.extend(_checked_probe(
                probe, "submodule driver configuration", accept=(0, 1),
            ).split("\x00"))
            pending.append((child, depth + 1))
    return driver_overrides(keys)
