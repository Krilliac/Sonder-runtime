"""Static-analysis gate and shrink-only ratchets.

Four checks, one baseline file (``lint_baseline.json`` beside this script):

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

4. **Server shim ratchet**: ``server_shims`` is a *set of names*, not a count,
   so swapping one protected name for another fails.  A shim is a top-level
   function of ``server.py`` whose whole body forwards to a packaged module
   through a ``deps=`` bundle (``return _campaigns.run(..., deps=_DEPS)``);
   a name that merely re-exports moved code (``from sonder_runtime... import
   name`` or ``name = alias.attr``) may also be listed.  The check fails when
   the AST finds a thin-wrapper shim that is not listed, and when a listed name
   is neither a thin wrapper nor a re-export any more.  Names are added only by
   the extraction slice that creates the shim (``--add-shim NAME``, reviewed)
   and leave the set when a retirement batch removes them (``--update`` prunes
   names that no longer qualify, and never adds one).  The set starts empty.

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
import ast
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
SERVER_SOURCE = "server.py"
SHIMS_KEY = "server_shims"
PACKAGED_ROOT = "sonder_runtime"
# A shim forwards to the moved implementation with its dependency bundle.
SHIM_BUNDLE_KEYWORD = "deps"


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


def _baseline_sizes(baseline: dict) -> dict[str, int]:
    return dict(baseline["module_lines"])


def _module_sizes() -> dict[str, int]:
    sizes = {}
    for name in SIZE_RATCHET_MODULES:
        path = REPO_ROOT / name
        if path.is_file():
            with path.open("rb") as handle:
                sizes[name] = sum(1 for _ in handle)
    return sizes


def _packaged_aliases(tree: ast.Module) -> set[str]:
    """Names the module binds to a ``sonder_runtime`` module or to a name inside one."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(
                alias.asname or alias.name.split(".")[0]
                for alias in node.names
                if alias.name.split(".")[0] == PACKAGED_ROOT
            )
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == PACKAGED_ROOT:
                names.update(alias.asname or alias.name for alias in node.names)
    return names


def _body_without_docstring(function: ast.AST) -> list[ast.stmt]:
    body = list(function.body)
    if (
        body and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return body


def find_server_shims(tree: ast.Module) -> set[str]:
    """Top-level functions that only forward to a packaged module with a ``deps=`` bundle."""
    aliases = _packaged_aliases(tree)
    shims: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = _body_without_docstring(node)
        if len(body) != 1:
            continue
        statement = body[0]
        value = statement.value if isinstance(statement, (ast.Return, ast.Expr)) else None
        if isinstance(value, ast.Await):
            value = value.value
        if not isinstance(value, ast.Call):
            continue
        func = value.func
        if (
            isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name)
            and func.value.id in aliases
            and any(keyword.arg == SHIM_BUNDLE_KEYWORD for keyword in value.keywords)
        ):
            shims.add(node.name)
    return shims


def find_server_reexports(tree: ast.Module) -> set[str]:
    """Top-level names bound straight to a packaged module or to a name inside one."""
    aliases = _packaged_aliases(tree)
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            if node.module.split(".")[0] == PACKAGED_ROOT:
                names.update(alias.asname or alias.name for alias in node.names)
        elif isinstance(node, ast.Assign):
            value = node.value
            direct = isinstance(value, ast.Name) and value.id in aliases
            nested = (
                isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name)
                and value.value.id in aliases
            )
            if direct or nested:
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
    return names


def _server_shims() -> tuple[set[str], set[str]]:
    """``(thin-wrapper shims, qualifying re-exports)`` found in ``server.py`` (empty when absent)."""
    path = REPO_ROOT / SERVER_SOURCE
    if not path.is_file():
        return set(), set()
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=SERVER_SOURCE)
    return find_server_shims(tree), find_server_reexports(tree)


def _load_baseline() -> dict:
    if not BASELINE_PATH.is_file():
        return {"lint": {}, "module_lines": {}, SHIMS_KEY: []}
    data = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data.get("lint"), dict) or not isinstance(data.get("module_lines"), dict):
        raise RuntimeError("%s is malformed" % BASELINE_PATH.name)
    shims = data.setdefault(SHIMS_KEY, [])
    if not isinstance(shims, list) or not all(isinstance(name, str) and name for name in shims):
        raise RuntimeError("%s: %s must be a list of names" % (BASELINE_PATH.name, SHIMS_KEY))
    return data


def _write_baseline(
    lint: dict[str, int], sizes: dict[str, int], previous: dict, *, allow_raise: bool = False,
    shims: set[str] | None = None,
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
        SHIMS_KEY: sorted(previous.get(SHIMS_KEY, []) if shims is None else shims),
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
    parser.add_argument(
        "--add-shim", action="append", default=[], metavar="NAME",
        help=(
            "list a server.py name in server_shims; it must currently be a thin "
            "wrapper or re-export of packaged code (the extraction slice that "
            "creates the shim adds it, and the reviewer checks it)"
        ),
    )
    args = parser.parse_args(argv)
    if args.update and args.rebaseline:
        parser.error("--update and --rebaseline are mutually exclusive")
    if args.add_shim and (args.update or args.rebaseline):
        parser.error("--add-shim cannot be combined with --update or --rebaseline")
    try:
        blocking = _ruff(BLOCKING_RULES, exclude_tests=True)
        current = _counts(_ruff(RATCHET_RULES, exclude_tests=False))
        baseline = _load_baseline()
    except (RuntimeError, OSError, ValueError) as exc:
        print("lint ratchet: %s" % exc, file=sys.stderr)
        return 2
    sizes = _module_sizes()
    try:
        wrappers, reexports = _server_shims()
    except (OSError, SyntaxError, ValueError) as exc:
        print("lint ratchet: could not read %s: %s" % (SERVER_SOURCE, exc), file=sys.stderr)
        return 2
    if args.add_shim:
        unknown = sorted(name for name in args.add_shim if name not in wrappers | reexports)
        if unknown:
            print(
                "refusing --add-shim: not a thin wrapper or re-export of packaged "
                "code in %s: %s" % (SERVER_SOURCE, ", ".join(unknown)),
                file=sys.stderr,
            )
            return 1
        listed_now = set(baseline[SHIMS_KEY])
        _write_baseline(
            baseline["lint"], _baseline_sizes(baseline), baseline, allow_raise=True,
            shims=listed_now | set(args.add_shim),
        )
        print("server_shims: added %s" % ", ".join(sorted(set(args.add_shim) - listed_now)))
        return 0
    listed = set(baseline[SHIMS_KEY])
    unlisted_shims = sorted(wrappers - listed)
    stale_shims = sorted(listed - (wrappers | reexports))
    kept_shims = listed & (wrappers | reexports)
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
    for name in unlisted_shims:
        violations.append(
            "server shim %s: thin wrapper of packaged code is not in server_shims "
            "(the extraction slice that creates it adds it with --add-shim)" % name
        )
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
        _write_baseline(current, sizes, baseline, allow_raise=True, shims=kept_shims)
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
        _write_baseline(current, sizes, baseline, shims=kept_shims)
        print("lint baseline updated: %d findings in %d buckets" % (
            sum(current.values()), len(current)))
        return 0
    for name in stale_shims:
        violations.append(
            "server shim %s: listed in server_shims but no longer a thin wrapper "
            "or re-export (a retirement batch removes it; --update prunes it)" % name
        )
    for line in violations:
        print(line)
    return 1 if violations else 0


if __name__ == "__main__":
    sys.exit(main())
