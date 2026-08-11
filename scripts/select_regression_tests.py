#!/usr/bin/env python3
"""Select the regression test set for a change from the change itself.

Why this exists
---------------
The previous selection rule for this repo was prose: "every test file that
references ``_AUTOPILOT_*``, ``tool_manifest``, ``AGENT_TOOL_HELP``,
``_loop_dispatch`` or ``workflow``".  That is a list of terms someone thought
of while writing the change, so it can only cover the surfaces they already had
in mind.  It selected 61 of 280 test files for a change that moved three tools
across the repository read-only gate and never once selected
``tests/test_read_only_agent_policy.py`` -- the file that tests exactly that
gate -- because that file contains none of the five hand-picked terms.  The set
passed by luck, not by coverage.

The rule here derives its search terms from the diff instead: every module-level
symbol the change actually touched, plus the production functions that consume
those symbols.  A test is selected when it *references* one of them.  That
cannot miss a surface the change hit, because the surface's own name is what
does the selecting.

Two defects this file was rebuilt to fix (#55)
----------------------------------------------
1. **It never diffed a commit range.**  The old ``changed_diff`` read
   ``git rev-parse HEAD`` into ``upstream`` and then used that variable only
   for its truthiness, diffing ``git diff HEAD`` -- the working tree against
   itself.  Committed work was therefore *invisible*: measured on this branch,
   a 53-line committed change to ``server.py`` yielded **0 identifiers and
   0 of 321 test files (exit 2)**, while the identical change seen through a
   real range yielded **9 identifiers and 59 of 321**.  Any lane that committed
   before selecting was selecting against an empty diff, so every
   "selected N of M" figure produced that way is a floor, not a measurement.
   Resolution of the base is now explicit, reported, and refuses to guess
   silently -- see ``resolve_base``.

2. **Selection was token-driven, not symbol-driven.**  The old ``scan_tests``
   matched ``\\bTERM\\b`` against each test file's raw *text*, so comments,
   docstrings and English prose selected files.  Measured: a two-line edit
   inside ``check()`` in ``scripts/check_error_signals.py`` selected **89 of
   321** test files, and the single English word ``check`` accounted for
   **79** of them -- without it the same change selects 17.  Matching is now
   done over each test file's AST, against names it genuinely references.

Usage
-----
    python scripts/select_regression_tests.py                # auto-detect base
    python scripts/select_regression_tests.py --since main   # explicit base
    python scripts/select_regression_tests.py --format args  # paste into pytest

Exit codes
----------
0   a usable selection.
2   VACUOUS -- empty range, no identifiers extracted, or no test selected.
    An infrastructure failure, never "nothing to run".
3   OVER-BROAD -- the selection is at or above ``--max-fraction`` of the suite
    and has stopped discriminating.  A selector that returns everything is
    trivially "correct" and useless, so it fails loudly rather than being
    quoted as coverage.
"""
from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
from pathlib import Path

# Directories whose contents are tests (searched), and directories that are
# never sources of change we should key on.
TEST_DIRS = ("tests", "proposals")
IGNORED_PREFIXES = ("app/build/",)

IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")

# Identifiers too generic to select on.  Kept short and boring: anything
# domain-specific belongs in the selection, not here.  Note that this list is
# NOT what stops the blowup -- AST matching is.  ``check`` is a real
# module-level symbol and is deliberately still selectable.
STOPWORDS = frozenset("""
self none true false return import from class None True False
value name text args kwargs error result output data item items
list dict set str int bool float tuple frozenset
if elif else for while with try except finally raise assert
def lambda yield await async global nonlocal pass break continue
test tests the and not for that this with when then
""".split())

# Call names whose *string* argument is really an attribute reference.  Tests
# in this repo reach production symbols almost entirely through
# ``monkeypatch.setattr(server, "file_read", ...)``, which an AST walk would
# otherwise not see as a reference to ``file_read`` at all.
_STRING_ATTR_CALLS = frozenset({
    "setattr", "getattr", "hasattr", "delattr", "patch", "object",
})


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ("git", "-C", str(repo)) + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit("git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def _try_git(repo: Path, *args: str) -> str | None:
    proc = subprocess.run(
        ("git", "-C", str(repo)) + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout.strip() if proc.returncode == 0 else None


def resolve_base(repo: Path, since: str | None) -> tuple[str, str]:
    """Resolve the commit this branch's work should be diffed against.

    Returns ``(sha, how)``.  The old code resolved this to ``HEAD`` itself and
    then diffed ``HEAD`` against the working tree, which is why committed work
    was invisible.  Guessing silently is the defect, so every rule that fires
    is named in the output and an unresolvable base is a hard failure.

    Order, most specific first:

    ``--since``
        Explicit operator intent; always wins.
    branch creation point
        ``git reflog show <branch>`` records ``branch: Created from <ref>`` for
        a branch made by ``git worktree add -b`` or ``git checkout -b``.  For a
        fleet lane this is exactly the fork point, and it is the only rule that
        gets it right here: this lane forks at ``06c2f79`` while
        ``merge-base(HEAD, main)`` is 37 commits further back and would drag in
        the whole feature branch.
    ``@{upstream}``
        A configured tracking branch.
    ``origin/HEAD`` / ``main`` / ``master``
        Last resort for a branch with no recorded fork point.
    """
    if since:
        sha = _try_git(repo, "rev-parse", "--verify", "%s^{commit}" % since)
        if not sha:
            raise SystemExit("--since %s does not name a commit" % since)
        merge_base = _try_git(repo, "merge-base", sha, "HEAD") or sha
        return merge_base, "--since %s" % since

    branch = _try_git(repo, "rev-parse", "--abbrev-ref", "HEAD")
    if branch and branch != "HEAD":
        reflog = _try_git(repo, "reflog", "show", "--no-abbrev", branch)
        if reflog:
            lines = [line for line in reflog.splitlines() if line.strip()]
            if lines and ": branch: Created from" in lines[-1]:
                sha = lines[-1].split()[0]
                verified = _try_git(repo, "rev-parse", "--verify", "%s^{commit}" % sha)
                if verified:
                    merge_base = _try_git(repo, "merge-base", verified, "HEAD")
                    if merge_base:
                        return merge_base, "branch creation point (reflog)"

    for candidate, label in (
        ("@{upstream}", "@{upstream}"),
        ("origin/HEAD", "origin/HEAD"),
        ("main", "main"),
        ("master", "master"),
    ):
        sha = _try_git(repo, "rev-parse", "--verify", "%s^{commit}" % candidate)
        if not sha:
            continue
        merge_base = _try_git(repo, "merge-base", sha, "HEAD")
        if merge_base:
            return merge_base, "merge-base with %s" % label

    raise SystemExit(
        "SELECTION VACUOUS: cannot resolve a base commit to diff against. "
        "Pass --since <rev> explicitly. Refusing to diff HEAD against itself, "
        "which is what silently hid every committed change before this fix."
    )


def changed_diff(repo: Path, base: str) -> str:
    """Unified diff of ``base..HEAD`` plus anything still in the working tree.

    Both halves matter: a lane that has committed needs the range, and a lane
    mid-edit needs the tree.  The old version emitted only the second half.
    """
    return run_git(repo, "diff", "-U0", base, "HEAD") + run_git(
        repo, "diff", "-U0", "HEAD",
    )


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def module_api_symbols(path: Path) -> tuple[set[str], list[tuple[int, int, str]]]:
    """Module-level names a test could reference, and their line spans.

    Only module-level bindings count.  A test cannot reference a local
    variable, so selecting on one (``line``, ``path``, ``name`` ...) matches
    everything and destroys the signal.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return set(), []
    symbols: set[str] = set()
    spans: list[tuple[int, int, str]] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            symbols.add(node.name)
            spans.append((node.lineno, getattr(node, "end_lineno", node.lineno), node.name))
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    symbols.add(target.id)
                    spans.append((
                        node.lineno, getattr(node, "end_lineno", node.lineno), target.id,
                    ))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            symbols.add(node.target.id)
            spans.append((
                node.lineno, getattr(node, "end_lineno", node.lineno), node.target.id,
            ))
    return symbols, spans


def parse_diff(repo: Path, diff: str) -> tuple[set[str], set[str], set[Path]]:
    """Return (changed modules, changed API identifiers, changed paths).

    An identifier is kept only when it is a module-level name of a changed
    source file -- either referenced on a changed line, or the definition the
    changed line sits inside.  That is precisely the set a test can name.
    """
    modules: set[str] = set()
    identifiers: set[str] = set()
    paths: set[Path] = set()
    current_path: Path | None = None
    api: set[str] = set()
    spans: list[tuple[int, int, str]] = []
    new_line_number = 0

    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[4:].strip()
            if path.startswith(("a/", "b/")):
                path = path[2:]
            normalized = path.replace("\\", "/")
            if (
                path != "/dev/null"
                and normalized.endswith(".py")
                and not normalized.startswith(TEST_DIRS)
                and not normalized.startswith(IGNORED_PREFIXES)
            ):
                current_path = repo / normalized
                modules.add(Path(normalized).stem)
                paths.add(current_path)
                api, spans = module_api_symbols(current_path)
            else:
                current_path, api, spans = None, set(), []
            continue
        if current_path is None:
            continue
        hunk = HUNK_RE.match(line)
        if hunk:
            new_line_number = int(hunk.group(1))
            continue
        if line.startswith("+") and not line.startswith("+++"):
            code = line[1:].split("#", 1)[0]
            for match in IDENTIFIER_RE.findall(code):
                if match in api and match.lower() not in STOPWORDS:
                    identifiers.add(match)
            for start, end, name in spans:
                if start <= new_line_number <= end:
                    identifiers.add(name)
            new_line_number += 1
        elif line.startswith("-") and not line.startswith("---"):
            # A deleted line's own numbering does not advance the new file, but
            # its identifiers still name surfaces the change touched.
            code = line[1:].split("#", 1)[0]
            for match in IDENTIFIER_RE.findall(code):
                if match in api and match.lower() not in STOPWORDS:
                    identifiers.add(match)
        elif line.startswith(" "):
            new_line_number += 1
    return modules, identifiers, paths


def consumer_symbols(paths: set[Path], identifiers: set[str]) -> set[str]:
    """Module-level definitions that *use* a changed symbol -- one hop.

    A test rarely names the private helper a change touched; it names the
    caller.  ``_agent_project_root_refusal`` is referenced by no test in this
    repo, but ``_agent_dispatch`` (its only caller) is referenced by many, and
    those are the tests that would catch a mistake in it.  Bounded to one hop
    and to the files the change already touched, so it cannot cascade.
    """
    consumers: set[str] = set()
    for path in sorted(paths):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except (OSError, SyntaxError):
            continue
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if node.name in identifiers:
                continue
            for inner in ast.walk(node):
                if isinstance(inner, ast.Name) and inner.id in identifiers:
                    consumers.add(node.name)
                    break
                if isinstance(inner, ast.Attribute) and inner.attr in identifiers:
                    consumers.add(node.name)
                    break
    return consumers - STOPWORDS


def _attribute_root(node: ast.Attribute) -> str | None:
    current: ast.AST = node
    while isinstance(current, ast.Attribute):
        current = current.value
    return current.id if isinstance(current, ast.Name) else None


def test_file_references(path: Path) -> set[str]:
    """Names a test file genuinely REFERENCES, taken from its AST.

    This is the fix for the token blowup.  The old rule scanned raw text with
    ``\\bTERM\\b``, so a symbol that is also an English word selected on prose:
    ``check`` matched 79 of 321 files, most of them in comments and docstrings
    and in ``self.check(...)`` calls on unrelated objects.  An AST walk sees
    neither comments nor docstrings, and attribute access counts only when its
    root is a name the file imported as a module -- ``server.file_read`` does,
    ``result.check`` does not.

    String arguments to ``setattr``/``getattr``/``patch`` are included because
    that is how this suite reaches production symbols
    (``monkeypatch.setattr(server, "file_read", ...)``); dropping them would
    lose real references, which is the dangerous direction.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except (OSError, SyntaxError):
        return set()

    module_aliases: set[str] = set()
    references: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                references.add(root)
                module_aliases.add(alias.asname or root)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                references.add(root)
                module_aliases.add(root)
            for alias in node.names:
                references.add(alias.name)
                module_aliases.add(alias.asname or alias.name)

    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            references.add(node.id)
        elif isinstance(node, ast.Attribute):
            root = _attribute_root(node)
            if root is not None and root in module_aliases:
                references.add(node.attr)
        elif isinstance(node, ast.Call):
            called = (
                node.func.attr if isinstance(node.func, ast.Attribute)
                else getattr(node.func, "id", "")
            )
            if called in _STRING_ATTR_CALLS:
                for argument in node.args:
                    if isinstance(argument, ast.Constant) and isinstance(
                        argument.value, str
                    ):
                        references.add(argument.value)
    return references


def scan_tests(repo: Path, terms: set[str]) -> tuple[dict[str, set[str]], list[str]]:
    """Map test path -> changed terms it references, parsing each file once."""
    selected: dict[str, set[str]] = {}
    all_tests: list[str] = []
    for directory in TEST_DIRS:
        root = repo / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("test_*.py")):
            relative = path.relative_to(repo).as_posix()
            all_tests.append(relative)
            if not terms:
                continue
            hits = test_file_references(path) & terms
            if hits:
                selected[relative] = hits
    return selected, all_tests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--since", default=None,
                        help="base rev; diffs merge-base(<since>, HEAD)..HEAD "
                             "plus the working tree")
    parser.add_argument("--format", choices=("list", "args"), default="list")
    parser.add_argument("--max-fraction", type=float, default=0.5,
                        help="fail (exit 3) when the selection reaches this "
                             "fraction of the suite and has stopped selecting")
    parser.add_argument("--show-uncovered", action="store_true", default=True)
    arguments = parser.parse_args()

    repo = Path(arguments.repo).resolve()
    base, how = resolve_base(repo, arguments.since)
    diff = changed_diff(repo, base)
    modules, identifiers, paths = parse_diff(repo, diff)
    consumers = consumer_symbols(paths, identifiers) if identifiers else set()

    # The changed API names plus their one-hop consumers are the selection.  A
    # bare module name is only a fallback: for a package like this one nearly
    # every test imports ``server``, so keying on it stops being a selection.
    terms = (identifiers | consumers) or modules
    fallback = not identifiers

    print(
        "# base %s (%s), %s commit(s) in range"
        % (base[:12], how,
           run_git(repo, "rev-list", "--count", "%s..HEAD" % base).strip()),
        file=sys.stderr,
    )

    if not terms:
        print(
            "SELECTION VACUOUS: the diff yielded no identifiers. This is an "
            "infrastructure failure (empty range? wrong --since?), not a clean "
            "result -- do not read it as 'no tests needed'.",
            file=sys.stderr,
        )
        return 2

    selected, all_tests = scan_tests(repo, terms)

    if not selected:
        print(
            "SELECTION VACUOUS: %d changed identifiers matched 0 of %d test "
            "files. Do not read this as 'nothing to run'." % (len(terms), len(all_tests)),
            file=sys.stderr,
        )
        return 2

    covered_terms: set[str] = set()
    for hits in selected.values():
        covered_terms |= hits
    uncovered = sorted(identifiers - covered_terms)

    if arguments.format == "args":
        print(" ".join(sorted(selected)))
    else:
        for path in sorted(selected):
            print(path)

    print(
        "\n# selected %d of %d test files from %d changed identifier(s) "
        "+ %d consumer(s) across %d module(s): %s%s"
        % (len(selected), len(all_tests), len(identifiers), len(consumers),
           len(modules), ", ".join(sorted(modules)),
           "  [FALLBACK: no API identifier changed, keyed on module name]"
           if fallback else ""),
        file=sys.stderr,
    )
    print(
        "# changed identifiers: %s" % ", ".join(sorted(identifiers)),
        file=sys.stderr,
    )
    if arguments.show_uncovered and uncovered:
        print(
            "# %d changed identifier(s) NO test file mentions -- the selected "
            "set cannot cover these:\n#   %s"
            % (len(uncovered), ", ".join(uncovered[:40])
               + (" ..." if len(uncovered) > 40 else "")),
            file=sys.stderr,
        )

    fraction = len(selected) / len(all_tests) if all_tests else 1.0
    if fraction >= arguments.max_fraction:
        print(
            "SELECTION OVER-BROAD: %d of %d test files (%.0f%%) is at or above "
            "--max-fraction %.2f. A selection this size is not discriminating "
            "and must not be quoted as targeted coverage -- run the full suite "
            "and say so, or narrow the change."
            % (len(selected), len(all_tests), fraction * 100,
               arguments.max_fraction),
            file=sys.stderr,
        )
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
