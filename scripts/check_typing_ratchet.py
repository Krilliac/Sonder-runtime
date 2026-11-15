"""Typing ratchet: a static type check that only tightens.

Every ``.py`` file under ``sonder_runtime/`` is in exactly one of two lists
kept beside this script:

* ``typing_strict_modules.txt`` -- modules that pass ``mypy --strict``.  They
  must keep passing: any error reported in one of them fails the gate.
* ``typing_baseline.txt`` -- the legacy modules that are not strict yet.  The
  list may only shrink.  A module leaves it when it is fixed (``--update``
  promotes every baseline module that now passes) or deleted.

A new file that is in neither list fails the gate: write it so it passes
``--strict`` and add it to the strict list (``--update`` does that for new files
that pass).  Nothing new ever enters the baseline.

Checks, in order:

1. Every listed path exists, and no path is in both lists.
2. Every file under ``sonder_runtime/`` is listed (the "new untyped module" check).
3. ``mypy --strict`` reports no error in any strict module.
4. With ``--against REF``: the baseline holds nothing that REF's baseline did
   not, and no module that is strict at REF dropped out of the strict list
   while its file still exists.  This is what makes the lists one-way in review.

``mypy.ini`` carries the shared settings; strictness comes from ``--strict``
on the command line and is applied only to the allowlist, so run this script
rather than bare mypy.  A module that shares its name with a package
(``x.py`` beside ``x/__init__.py``) is dead code Python never imports and mypy
cannot check, so it is outside the ratchet.

Exit status: 0 ok, 1 violations, 2 tool failure (mypy missing, crashed, or
produced output this script cannot read).
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "sonder_runtime"
STRICT_PATH = Path(__file__).with_name("typing_strict_modules.txt")
BASELINE_PATH = Path(__file__).with_name("typing_baseline.txt")
CONFIG_NAME = "mypy.ini"
PLATFORMS = ("linux", "win32")

_ERROR_LINE = re.compile(r"^(?P<path>[^\s:][^:]*\.py):(?P<line>\d+)(?::\d+)?: error: (?P<message>.*)$")
_STRICT_HEADER = (
    "# Modules that pass `mypy --strict` and must keep passing (see scripts/check_typing_ratchet.py).",
    "# Grow this list; removing a module whose file still exists fails the gate.",
)
_BASELINE_HEADER = (
    "# Legacy modules that are not strict yet. Shrink-only: never add a line.",
    "# `python scripts/check_typing_ratchet.py --update` drops modules that now pass or were deleted.",
)


class RatchetError(RuntimeError):
    """The gate could not run (mypy absent, crashed, or unparseable output)."""


def discover_modules(root: Path = REPO_ROOT, package: str = PACKAGE) -> list[str]:
    """Return every checkable ``.py`` path under *package*, posix-style and sorted."""
    found = []
    for path in sorted((root / package).rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if path.name != "__init__.py" and path.with_suffix("").joinpath("__init__.py").is_file():
            continue
        found.append(path.relative_to(root).as_posix())
    return found


def read_list(path: Path) -> list[str]:
    if not path.is_file():
        return []
    entries = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            entries.append(line)
    return entries


def write_list(path: Path, header: Sequence[str], entries: Sequence[str]) -> None:
    body = "\n".join([*header, *sorted(set(entries))]) + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")


def parse_mypy_output(text: str) -> dict[str, list[str]]:
    """Group ``error:`` lines by posix path; ``note:`` lines are ignored."""
    errors: dict[str, list[str]] = defaultdict(list)
    for raw in text.splitlines():
        match = _ERROR_LINE.match(raw.strip())
        if match:
            path = match.group("path").replace("\\", "/")
            errors[path].append("%s:%s: %s" % (path, match.group("line"), match.group("message")))
    return dict(errors)


def _run_mypy_once(root: Path, platform: str) -> dict[str, list[str]]:
    command = [
        sys.executable, "-m", "mypy", "--strict",
        "--config-file", str(root / CONFIG_NAME),
        "--platform", platform,
        "--cache-dir", str(root / ".mypy_cache" / platform),
    ]
    try:
        completed = subprocess.run(
            command, cwd=root, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise RatchetError("could not run mypy: %s" % exc) from exc
    output = completed.stdout + completed.stderr
    if "No module named mypy" in output:
        raise RatchetError("mypy is not installed (pip install -r requirements-dev.txt)")
    if completed.returncode not in (0, 1):
        raise RatchetError("mypy failed (exit %d): %s" % (completed.returncode, output.strip()[-2000:]))
    errors = parse_mypy_output(completed.stdout)
    if completed.returncode == 1 and not errors:
        # Exit 1 means mypy found errors. If none parsed, the output format
        # changed and a silent pass would be a fabricated one.
        raise RatchetError("mypy reported errors this script could not read: %s" % output.strip()[-2000:])
    if completed.returncode == 0 and errors:
        raise RatchetError("mypy exited 0 but printed errors; refusing to trust the result")
    return errors


def run_mypy(root: Path = REPO_ROOT) -> dict[str, list[str]]:
    """Run ``mypy --strict`` for each platform and return the merged errors per file.

    A module must be clean on every platform in ``PLATFORMS``, so the verdict
    does not depend on which OS runs the gate (CI is Linux, development is
    Windows, and ``sys.platform`` branches change what mypy sees).
    """
    merged: dict[str, list[str]] = {}
    for platform in PLATFORMS:
        for path, lines in _run_mypy_once(root, platform).items():
            known = merged.setdefault(path, [])
            known.extend(line for line in lines if line not in known)
    return merged


def evaluate(
    modules: Sequence[str],
    strict: Sequence[str],
    baseline: Sequence[str],
    errors: Mapping[str, Sequence[str]],
) -> tuple[list[str], list[str]]:
    """Return ``(violations, promotable)`` for the current tree.

    ``promotable`` is every module that is not strict yet and has no mypy error:
    a baseline module that was fixed, or a new file written strict-clean.
    """
    present = set(modules)
    strict_set = set(strict)
    baseline_set = set(baseline)
    violations = []
    for path in sorted(strict_set - present):
        violations.append("strict list names %s, which does not exist (remove it)" % path)
    for path in sorted(baseline_set - present):
        violations.append("baseline names %s, which does not exist (remove it)" % path)
    for path in sorted(strict_set & baseline_set):
        violations.append("%s is in both the strict list and the baseline (remove it from the baseline)" % path)
    for path in modules:
        if path not in strict_set and path not in baseline_set:
            violations.append(
                "%s is new and not in the strict list: make it pass `mypy --strict` and add it "
                "to scripts/typing_strict_modules.txt (--update adds it if it passes)" % path
            )
    for path in sorted(strict_set & present):
        for line in errors.get(path, ()):
            violations.append("strict module regressed: %s" % line)
    promotable = [
        path for path in modules
        if path not in strict_set and path in present and not errors.get(path)
    ]
    return violations, promotable


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *args], cwd=root, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise RatchetError("could not run git: %s" % exc) from exc


def _verify_ref(root: Path, ref: str) -> None:
    completed = _git(root, "rev-parse", "--verify", "--quiet", "%s^{commit}" % ref)
    if completed.returncode != 0:
        raise RatchetError(
            "--against %s does not resolve to a commit (typo, shallow checkout or missing fetch?)" % ref
        )


def _git_show(root: Path, ref: str, relative: Path) -> list[str] | None:
    """Lines of *relative* at *ref*, or None when the ref resolves but has no such file.

    An unresolvable ref or any other git failure raises, so it cannot read as "no lists yet".
    """
    _verify_ref(root, ref)
    listing = _git(root, "ls-tree", "--name-only", ref, "--", relative.as_posix())
    if listing.returncode != 0:
        raise RatchetError("git ls-tree %s failed: %s" % (ref, listing.stderr.strip()))
    if not listing.stdout.strip():
        return None
    completed = _git(root, "show", "%s:%s" % (ref, relative.as_posix()))
    if completed.returncode != 0:
        raise RatchetError("git show %s:%s failed: %s" % (ref, relative.as_posix(), completed.stderr.strip()))
    return [
        line.strip() for line in completed.stdout.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


def compare_against(
    root: Path, ref: str, modules: Sequence[str], strict: Sequence[str], baseline: Sequence[str],
) -> list[str]:
    """One-way checks against the lists at *ref*. A ref without lists yet is not an error."""
    base_strict = _git_show(root, ref, STRICT_PATH.relative_to(REPO_ROOT))
    base_baseline = _git_show(root, ref, BASELINE_PATH.relative_to(REPO_ROOT))
    violations = []
    present = set(modules)
    if base_baseline is not None:
        for path in sorted(set(baseline) - set(base_baseline)):
            violations.append("baseline gained %s (the baseline may only shrink)" % path)
    if base_strict is not None:
        for path in sorted((set(base_strict) & present) - set(strict)):
            violations.append("%s was strict at %s and is no longer in the strict list" % (path, ref))
    return violations


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--update", action="store_true",
        help="promote passing modules into the strict list and drop deleted ones; never grows the baseline",
    )
    mode.add_argument(
        "--init", action="store_true",
        help="first-time setup: write both lists from the current tree (refuses if they exist)",
    )
    parser.add_argument(
        "--against", metavar="REF",
        help="also require the lists to be one-way relative to REF (a git ref such as origin/main)",
    )
    args = parser.parse_args(argv)
    try:
        modules = discover_modules()
        errors = run_mypy()
        if args.init:
            if STRICT_PATH.exists() or BASELINE_PATH.exists():
                print("typing ratchet: lists already exist; use --update", file=sys.stderr)
                return 1
            clean = [path for path in modules if not errors.get(path)]
            write_list(STRICT_PATH, _STRICT_HEADER, clean)
            write_list(BASELINE_PATH, _BASELINE_HEADER, [p for p in modules if errors.get(p)])
            print("typing ratchet: %d strict, %d baseline" % (len(clean), len(modules) - len(clean)))
            return 0
        strict = read_list(STRICT_PATH)
        baseline = read_list(BASELINE_PATH)
        if not strict:
            raise RatchetError("%s is empty or missing; run --init once" % STRICT_PATH.name)
        violations, promotable = evaluate(modules, strict, baseline, errors)
        if args.against:
            violations += compare_against(REPO_ROOT, args.against, modules, strict, baseline)
    except (RatchetError, OSError) as exc:
        print("typing ratchet: %s" % exc, file=sys.stderr)
        return 2
    if args.update:
        regressions = [v for v in violations if v.startswith("strict module regressed")]
        if regressions:
            print("refusing --update while a strict module has errors:", file=sys.stderr)
            for line in regressions:
                print("  " + line, file=sys.stderr)
            return 1
        present = set(modules)
        new_strict = sorted({p for p in strict if p in present} | set(promotable))
        new_baseline = sorted(p for p in baseline if p in present and p not in set(promotable))
        write_list(STRICT_PATH, _STRICT_HEADER, new_strict)
        write_list(BASELINE_PATH, _BASELINE_HEADER, new_baseline)
        print("typing ratchet updated: %d strict (+%d promoted), %d baseline" % (
            len(new_strict), len(promotable), len(new_baseline)))
        listed = set(new_strict) | set(new_baseline)
        unlisted = [path for path in modules if path not in listed]
        for path in unlisted:
            print("  %s is new and fails `mypy --strict`: fix it, then re-run --update" % path, file=sys.stderr)
        return 1 if unlisted else 0
    if violations:
        print("typing ratchet FAILED (%d):" % len(violations), file=sys.stderr)
        for line in violations:
            print("  " + line, file=sys.stderr)
        return 1
    print("typing ratchet ok: %d strict modules, %d baseline, %d promotable (run --update)" % (
        len(strict), len(baseline), len(promotable)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
