#!/usr/bin/env python3
"""Run the tests a change can affect, fast; or the whole suite, the same way.

The iteration loop this wraps is three commands people kept retyping
differently: pick the regression set from the diff
(``scripts/select_regression_tests.py``), run it in parallel with
work-stealing distribution, and re-run last time's failures first. It prints
the selector's uncovered-identifier report on every run, because a green
selected set says nothing about a changed identifier no test mentions.

Usage
-----
    python scripts/test_fast.py                    # change since merge-base with origin/main
    python scripts/test_fast.py --since HEAD~3     # change since a given ref
    python scripts/test_fast.py --working-tree     # uncommitted change only (vs HEAD)
    python scripts/test_fast.py --all              # the full suite, same flags
    python scripts/test_fast.py -- -k gate -x      # anything after -- goes to pytest
    python scripts/test_fast.py --dry-run          # print the pytest command only

Selection is for iteration. Run the full suite (``--all``) before merging.

Exit codes: pytest's own; 2 when the selection is vacuous (the selector's
infrastructure-failure signal, never "nothing to run"); 3 when the selector
itself fails.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SELECTOR = REPO / "scripts" / "select_regression_tests.py"
DEFAULT_UPSTREAM = "origin/main"
# Work-stealing lets idle workers take the remaining tests of one slow file,
# which matters most for a small selected set. On broad runs it measured no
# better than ``--dist load`` (docs/wiki/20-test-suite-performance.md), which
# is why CI keeps ``load``; pass ``-- --dist load`` to override here.
BASE_PYTEST_FLAGS = ("--dist", "worksteal", "--ff")


def parse_args(argv: list[str]) -> tuple[argparse.Namespace, list[str]]:
    """Split ``argv`` into this script's options and pytest passthrough.

    Everything after a literal ``--`` is passed through verbatim; unknown
    options before it are passed through too, so ``test_fast.py -x`` works.
    """
    if "--" in argv:
        split = argv.index("--")
        own, passthrough = argv[:split], argv[split + 1:]
    else:
        own, passthrough = argv, []
    parser = argparse.ArgumentParser(
        description="Run the regression set for a change (or --all) under xdist.",
    )
    scope = parser.add_mutually_exclusive_group()
    scope.add_argument("--all", action="store_true", help="run the full suite")
    scope.add_argument(
        "--since", metavar="REF",
        help="select from REF...HEAD plus the working tree "
             "(default: merge-base of HEAD and %s)" % DEFAULT_UPSTREAM,
    )
    scope.add_argument(
        "--working-tree", action="store_true",
        help="select from the uncommitted change only (HEAD vs working tree)",
    )
    parser.add_argument(
        "-n", "--workers", default="auto",
        help="xdist worker count (default: auto; use a small number on a busy host)",
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="print the pytest command and exit",
    )
    arguments, unknown = parser.parse_known_args(own)
    return arguments, unknown + passthrough


def merge_base(repo: Path, upstream: str = DEFAULT_UPSTREAM) -> str | None:
    """The merge-base of HEAD and ``upstream``, or None when unavailable."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "HEAD", upstream],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    sha = completed.stdout.strip()
    return sha if completed.returncode == 0 and sha else None


def resolve_since(arguments: argparse.Namespace, repo: Path) -> str | None:
    """The ``--since`` ref handed to the selector (None = selector default)."""
    if arguments.working_tree:
        return "HEAD"
    if arguments.since:
        return arguments.since
    return merge_base(repo)


def selector_command(python: str, since: str | None) -> list[str]:
    command = [python, str(SELECTOR), "--repo", str(REPO), "--format", "json"]
    if since:
        command += ["--since", since]
    return command


def pytest_command(
    python: str,
    workers: str,
    selected: list[str] | None,
    passthrough: list[str],
) -> list[str]:
    """The pytest invocation: defaults first, so passthrough flags win."""
    command = [python, "-m", "pytest", "-n", str(workers), *BASE_PYTEST_FLAGS]
    command += list(passthrough)
    if selected:
        command += list(selected)
    return command


def uncovered_report(selection: dict) -> str:
    """Human summary of what the selection cannot cover."""
    uncovered = list(selection.get("uncovered_identifiers") or ())
    lines = [
        "# test_fast: selected %d of %d test files"
        % (selection.get("selected_count", 0), selection.get("test_file_count", 0)),
    ]
    if selection.get("fallback_to_module"):
        lines.append("# FALLBACK: no API identifier changed; keyed on module names")
    if uncovered:
        lines.append(
            "# %d changed identifier(s) NO test mentions -- this run cannot cover them:"
            % len(uncovered)
        )
        lines.extend("#   %s" % name for name in uncovered)
    else:
        lines.append("# uncovered identifiers: none")
    return "\n".join(lines)


def select(python: str, since: str | None) -> tuple[int, dict | None]:
    completed = subprocess.run(
        selector_command(python, since), cwd=REPO, capture_output=True, text=True,
    )
    # The selector's own stderr summary is part of the report.
    if completed.stderr:
        sys.stderr.write(completed.stderr)
    if completed.returncode != 0:
        return completed.returncode, None
    try:
        return 0, json.loads(completed.stdout)
    except json.JSONDecodeError:
        print("test_fast: selector printed no JSON", file=sys.stderr)
        return 3, None


def main(argv: list[str] | None = None) -> int:
    arguments, passthrough = parse_args(list(sys.argv[1:] if argv is None else argv))
    python = sys.executable
    selected: list[str] | None = None
    if not arguments.all:
        since = resolve_since(arguments, REPO)
        status, selection = select(python, since)
        if selection is None:
            return 2 if status == 2 else 3
        selected = list(selection.get("selected") or ())
        print(uncovered_report(selection), file=sys.stderr)
        if not selected:  # pragma: no cover - the selector exits 2 first
            return 2
    command = pytest_command(python, arguments.workers, selected, passthrough)
    if arguments.dry_run:
        print(subprocess.list2cmdline(command) if os.name == "nt" else " ".join(command))
        return 0
    return subprocess.call(command, cwd=REPO)


if __name__ == "__main__":
    raise SystemExit(main())
