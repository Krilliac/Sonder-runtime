"""Static-analysis gate and shrink-only ratchets.

Three checks, one baseline file (``lint_baseline.json`` beside this script):

1. **Blocking rules** (``BLOCKING_RULES``) must have zero findings outside
   ``tests/``.  These are the classes that are runtime bugs rather than style:
   undefined names, undefined ``__all__`` exports, silently shadowed
   definitions, pylint errors, and closures over loop variables.  An audit
   found reachable ``NameError`` paths in exactly these classes.
2. **Lint ratchet**: for ``RATCHET_RULES`` everywhere (tests included), the
   finding count per ``(path, rule)`` may never exceed the baseline.  New
   files start at zero.  Fixing findings is always allowed; pass ``--update``
   to record the lower counts so they cannot creep back.
3. **Module-size ratchet**: listed legacy modules may never grow past their
   recorded line count.  ``server.py`` is the strangler-migration source;
   new behaviour belongs in ``sonder_runtime/``.  ``--update`` only ever
   lowers a limit.

``--rebaseline`` is the one explicit way to raise counts: it records the
current tree as the new baseline and lists every bucket and limit it raised,
so the rise is visible in review.  It is meant for integrating branches that
were each measured against an older base (their growth adds up only once they
are merged); it still refuses while any blocking finding exists.  CI never
passes it.

Ruff is invoked with explicit arguments so the gate cannot drift with a
local configuration file.  Exit status: 0 ok, 1 violations, 2 tool failure.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BASELINE_PATH = Path(__file__).with_name("lint_baseline.json")
BLOCKING_RULES = ("F821", "F822", "F811", "PLE", "B023")
RATCHET_RULES = ("F", "B", "PLE")
TARGET_VERSION = "py312"
SIZE_RATCHET_MODULES = (
    "server.py",
    "master_orchestrator.py",
    "adaptive_training.py",
    "selfmod.py",
    "sonder_launcher.py",
    "sonder_runtime/interfaces/http/serve.py",
)


def _ruff(select: tuple[str, ...], *, exclude_tests: bool) -> list[dict]:
    command = [
        sys.executable, "-m", "ruff", "check", ".",
        "--no-cache", "--isolated", "--exit-zero",
        "--output-format", "json",
        "--target-version", TARGET_VERSION,
        "--select", ",".join(select),
        "--extend-exclude", "eval_runs",
    ]
    if exclude_tests:
        command += ["--extend-exclude", "tests"]
    try:
        completed = subprocess.run(
            command, cwd=REPO_ROOT, capture_output=True, text=True, check=False,
        )
    except OSError as exc:
        raise RuntimeError("could not run ruff: %s" % exc) from exc
    if completed.returncode != 0:
        raise RuntimeError(
            "ruff failed (exit %d): %s" % (completed.returncode, completed.stderr.strip())
        )
    try:
        findings = json.loads(completed.stdout or "[]")
    except ValueError as exc:
        raise RuntimeError("ruff produced invalid JSON") from exc
    if not isinstance(findings, list):
        raise RuntimeError("ruff produced an unexpected report shape")
    return findings


def _relative(path: str) -> str:
    try:
        return Path(path).resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return Path(path).as_posix()


def _counts(findings: list[dict]) -> dict[str, int]:
    counter = Counter(
        "%s::%s" % (_relative(item["filename"]), item.get("code") or "syntax")
        for item in findings
    )
    return dict(sorted(counter.items()))


def _module_sizes() -> dict[str, int]:
    sizes = {}
    for name in SIZE_RATCHET_MODULES:
        path = REPO_ROOT / name
        if path.is_file():
            with path.open("rb") as handle:
                sizes[name] = sum(1 for _ in handle)
    return sizes


def _load_baseline() -> dict:
    if not BASELINE_PATH.is_file():
        return {"lint": {}, "module_lines": {}}
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data.get("lint"), dict) or not isinstance(data.get("module_lines"), dict):
        raise RuntimeError("%s is malformed" % BASELINE_PATH.name)
    return data


def _write_baseline(
    lint: dict[str, int], sizes: dict[str, int], previous: dict, *, allow_raise: bool = False,
) -> None:
    old_sizes = previous.get("module_lines", {})
    module_lines = {
        name: count if allow_raise else min(count, old_sizes.get(name, count))
        for name, count in sizes.items()
    }
    payload = {
        "_comment": (
            "Shrink-only. Regenerate with `python scripts/check_lint_ratchet.py "
            "--update` after fixing findings or shrinking a module; raising a "
            "count takes an explicit, reviewed `--rebaseline`."
        ),
        "rules": list(RATCHET_RULES),
        "lint": lint,
        "module_lines": dict(sorted(module_lines.items())),
    }
    BASELINE_PATH.write_text(json.dumps(payload, indent=1, sort_keys=False) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--update", action="store_true",
        help="record current counts; refuses to raise any lint count or module limit",
    )
    parser.add_argument(
        "--rebaseline", action="store_true",
        help=(
            "record the current tree as the baseline even where counts rose "
            "(integration merges only); lists every raised bucket and still "
            "refuses while blocking findings exist"
        ),
    )
    args = parser.parse_args(argv)
    if args.update and args.rebaseline:
        parser.error("--update and --rebaseline are mutually exclusive")
    try:
        blocking = _ruff(BLOCKING_RULES, exclude_tests=True)
        current = _counts(_ruff(RATCHET_RULES, exclude_tests=False))
        baseline = _load_baseline()
    except (RuntimeError, OSError, ValueError) as exc:
        print("lint ratchet: %s" % exc, file=sys.stderr)
        return 2
    sizes = _module_sizes()
    blocking_violations = []
    for item in blocking:
        location = item.get("location") or {}
        blocking_violations.append(
            "blocking %s %s:%s: %s" % (
                item.get("code"), _relative(item["filename"]),
                location.get("row", "?"), item.get("message", ""),
            )
        )
    violations = list(blocking_violations)
    raised = []
    allowed = baseline["lint"]
    for key, count in current.items():
        if count > allowed.get(key, 0):
            message = "lint ratchet %s: %d findings, baseline allows %d" % (
                key, count, allowed.get(key, 0))
            violations.append(message)
            raised.append(message)
    limits = baseline["module_lines"]
    for name, lines in sizes.items():
        if name in limits and lines > limits[name]:
            message = "module size %s: %d lines, limit %d" % (name, lines, limits[name])
            violations.append(message + " (move new code into sonder_runtime/)")
            raised.append(message)
    if args.rebaseline:
        if blocking_violations:
            print("refusing --rebaseline while blocking findings exist:", file=sys.stderr)
            for line in blocking_violations:
                print("  " + line, file=sys.stderr)
            return 1
        _write_baseline(current, sizes, baseline, allow_raise=True)
        print("lint baseline rebaselined: %d findings in %d buckets; raised %d:" % (
            sum(current.values()), len(current), len(raised)))
        for line in raised:
            print("  " + line)
        return 0
    if args.update:
        if blocking_violations or (violations and baseline["lint"]):
            print("refusing --update while the tree regresses:", file=sys.stderr)
            for line in violations:
                print("  " + line, file=sys.stderr)
            return 1
        _write_baseline(current, sizes, baseline)
        print("lint baseline updated: %d findings in %d buckets" % (
            sum(current.values()), len(current)))
        return 0
    for line in violations:
        print(line)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
