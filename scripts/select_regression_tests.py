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

The rule here derives its search terms from the diff instead: every identifier
the change actually touched.  A test is selected when it references any changed
identifier or any changed module.  That cannot miss a surface the change hit,
because the surface's own name is what does the selecting.

It also reports the inverse, which is the number that matters most: changed
identifiers that **no** test mentions at all.  A large selected set means
nothing if the specific thing you changed is in the uncovered list.

Usage
-----
    python scripts/select_regression_tests.py                # working tree vs HEAD
    python scripts/select_regression_tests.py --since main   # a whole branch
    python scripts/select_regression_tests.py --format args  # paste into pytest

Exit codes: 0 selected something, 2 the selection went vacuous (no identifiers
extracted, or no tests selected) -- which is an infrastructure failure, not a
clean result, and must never be read as "nothing to run".
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import time
from pathlib import Path

# Directories whose contents are tests (searched), and directories that are
# never sources of change we should key on.
TEST_DIRS = ("tests", "proposals")
IGNORED_PREFIXES = ("app/build/",)
MAX_UNTRACKED_SOURCE_BYTES = 4 * 1024 * 1024

# A changed line's identifiers.  Deliberately includes dunder-free private
# names (_AGENT_...), because those are exactly the policy constants whose
# movement the old rule kept missing.
IDENTIFIER_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
DELETED_DEFINITION_RE = re.compile(
    r"^(?:async\s+def|def|class)\s+([A-Za-z_][A-Za-z0-9_]*)\b"
)
DELETED_ASSIGNMENT_RE = re.compile(
    r"^([A-Za-z_][A-Za-z0-9_]*)\s*(?::[^=]+)?=(?!=)"
)

# Identifiers too generic to select on: matching them would select everything
# and hide the signal.  Keep this list short and boring -- anything domain
# specific belongs in the selection, not here.
STOPWORDS = frozenset("""
self none true false return import from class None True False
value name text args kwargs error result output data item items schema
list dict set str int bool float tuple frozenset
if elif else for while with try except finally raise assert
def lambda yield await async global nonlocal pass break continue
test tests the and not for that this with when then main check
""".split())


def run_git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ("git", "-C", str(repo)) + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit("git %s failed: %s" % (" ".join(args), proc.stderr.strip()))
    return proc.stdout


def try_git(repo: Path, *args: str) -> str | None:
    """Return Git output, or ``None`` for an unavailable revision/query."""
    proc = subprocess.run(
        ("git", "-C", str(repo)) + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    return proc.stdout if proc.returncode == 0 else None


def default_base(repo: Path) -> str:
    """Resolve a useful base without requiring every local branch to track one."""
    upstream = try_git(
        repo, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}",
    )
    if upstream and upstream.strip():
        return upstream.strip()
    current = (try_git(repo, "branch", "--show-current") or "").strip()
    for candidate in ("origin/main", "main", "origin/master", "master"):
        if candidate == current:
            continue
        if try_git(repo, "rev-parse", "--verify", "--quiet", candidate):
            return candidate
    # A dirty main checkout still has a meaningful working-tree diff.  Using
    # HEAD here is explicit and safe; a committed untracked branch should pass
    # --since rather than receiving a guessed historical base.
    return "HEAD"


def changed_diff(repo: Path, since: str | None) -> str:
    """Unified diff of the change under review, working tree included."""
    if since:
        return run_git(repo, "diff", "-U0", "%s...HEAD" % since) + run_git(
            repo, "diff", "-U0", "HEAD",
        ) + untracked_python_diff(repo)
    # Include commits not yet pushed, not merely a dirty working tree.  HEAD
    # always exists in a normal repository, so using it as the probe made this
    # branch silently vacuous after a commit.
    base = default_base(repo)
    return run_git(repo, "diff", "-U0", "%s...HEAD" % base) + run_git(
        repo, "diff", "-U0", "HEAD",
    ) + untracked_python_diff(repo)


def untracked_python_diff(repo: Path) -> str:
    """Render bounded untracked Python files as synthetic added-file diffs."""
    raw = run_git(
        repo, "ls-files", "-z", "--others", "--exclude-standard", "--", "*.py",
    )
    repo = repo.resolve()
    chunks: list[str] = []
    for name in raw.split("\0"):
        if not name:
            continue
        relative = Path(name)
        path = repo / relative
        try:
            path.resolve().relative_to(repo)
        except (OSError, ValueError) as exc:
            raise SystemExit("untracked source escapes repository: %s" % name) from exc
        if path.is_symlink() or not path.is_file():
            raise SystemExit("untracked source is not a regular file: %s" % name)
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise SystemExit("cannot inspect untracked source: %s" % name) from exc
        if size > MAX_UNTRACKED_SOURCE_BYTES:
            raise SystemExit(
                "untracked source exceeds %d-byte selection limit: %s"
                % (MAX_UNTRACKED_SOURCE_BYTES, name)
            )
        source = path.read_text(encoding="utf-8", errors="replace")
        lines = source.splitlines()
        normalized = relative.as_posix()
        chunks.extend([
            "--- /dev/null",
            "+++ b/%s" % normalized,
            "@@ -0,0 +1,%d @@" % len(lines),
            *("+" + line for line in lines),
        ])
    return ("\n".join(chunks) + "\n") if chunks else ""


HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def module_api_symbols(path: Path) -> tuple[set[str], list[tuple[int, int, str]]]:
    """Module-level names a test could reference, and their line spans.

    Only module-level bindings count.  A test cannot reference a local
    variable, so selecting on one (``line``, ``path``, ``name`` ...) matches
    everything and destroys the signal -- which is what a raw word scan of the
    changed lines does: it also picks up English from comments and docstrings
    ("Advertise", "Deriving", "restating") and selected 304 of 313 files.
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


def parse_diff(repo: Path, diff: str) -> tuple[set[str], set[str]]:
    """Return (changed modules, changed API identifiers) from a unified diff.

    An identifier is kept only when it is a module-level name of a changed
    source file -- either referenced on a changed line, or the definition the
    changed line sits inside.  That is precisely the set a test can name.
    """
    modules: set[str] = set()
    identifiers: set[str] = set()
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
                if (
                    start <= new_line_number <= end
                    and name.lower() not in STOPWORDS
                ):
                    identifiers.add(name)
            new_line_number += 1
        elif line.startswith("-") and not line.startswith("---"):
            # A deleted line's own numbering does not advance the new file, but
            # its identifiers still name surfaces the change touched.
            code = line[1:].split("#", 1)[0]
            stripped = code.strip()
            deleted_binding = (
                DELETED_DEFINITION_RE.match(stripped)
                or (
                    DELETED_ASSIGNMENT_RE.match(stripped)
                    if code == code.lstrip() else None
                )
            )
            if (
                deleted_binding
                and deleted_binding.group(1).lower() not in STOPWORDS
            ):
                identifiers.add(deleted_binding.group(1))
            for match in IDENTIFIER_RE.findall(code):
                if match in api and match.lower() not in STOPWORDS:
                    identifiers.add(match)
            for start, end, name in spans:
                if (
                    start <= new_line_number <= end
                    and name.lower() not in STOPWORDS
                ):
                    identifiers.add(name)
        elif line.startswith(" "):
            new_line_number += 1
    return modules, identifiers


def changed_test_paths(diff: str) -> set[str]:
    """Return non-deleted test files named by the diff itself.

    A test-only change has no production identifier to search for, but the
    changed test is always part of the minimum safe regression set.
    """
    paths: set[str] = set()
    for line in diff.splitlines():
        if not line.startswith("+++ "):
            continue
        path = line[4:].strip()
        if path.startswith(("a/", "b/")):
            path = path[2:]
        normalized = path.replace("\\", "/")
        if normalized == "/dev/null":
            continue
        candidate = Path(normalized)
        if (
            candidate.name.startswith("test_")
            and candidate.suffix == ".py"
            and candidate.parts
            and candidate.parts[0] in TEST_DIRS
        ):
            paths.add(candidate.as_posix())
    return paths


def scan_tests(repo: Path, terms: set[str]) -> tuple[dict[str, set[str]], list[str]]:
    """Map test path -> terms it references, streaming each file once."""
    selected: dict[str, set[str]] = {}
    all_tests: list[str] = []
    pattern = re.compile(
        r"\b(?:%s)\b" % "|".join(sorted(map(re.escape, terms), key=len, reverse=True))
    ) if terms else None
    for directory in TEST_DIRS:
        root = repo / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("test_*.py")):
            relative = path.relative_to(repo).as_posix()
            all_tests.append(relative)
            if pattern is None:
                continue
            hits: set[str] = set()
            try:
                with path.open("r", encoding="utf-8", errors="replace") as handle:
                    for line in handle:
                        hits.update(pattern.findall(line))
            except OSError:
                continue
            if hits:
                selected[relative] = hits
    return selected, all_tests


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", default=".", help="repository root")
    parser.add_argument("--since", default=None,
                        help="base rev; compares <since>...HEAD plus working tree")
    parser.add_argument("--format", choices=("list", "args", "json"), default="list")
    parser.add_argument("--show-uncovered", action="store_true", default=True)
    arguments = parser.parse_args()

    repo = Path(arguments.repo).resolve()
    started = time.perf_counter()
    diff = changed_diff(repo, arguments.since)
    modules, identifiers = parse_diff(repo, diff)
    direct_tests = changed_test_paths(diff)
    # The changed API names are the selection.  A bare module name is only a
    # fallback: for a package like this one nearly every test imports
    # ``server``, so keying on it selects 129 of 313 files and stops being a
    # selection at all.  The 18 changed symbols alone select 63 -- including
    # test_read_only_agent_policy.py, on four separate symbols.
    terms = identifiers or modules
    fallback = not identifiers

    if not terms and not direct_tests:
        print(
            "SELECTION VACUOUS: the diff yielded no identifiers. This is an "
            "infrastructure failure (empty diff? wrong --since?), not a clean "
            "result -- do not read it as 'no tests needed'.",
            file=sys.stderr,
        )
        return 2

    selected, all_tests = scan_tests(repo, terms)
    all_test_set = set(all_tests)
    direct_tests &= all_test_set
    for path in direct_tests:
        selected.setdefault(path, set()).add("<changed-test>")

    if not selected:
        print(
            "SELECTION VACUOUS: %d changed identifiers matched 0 of %d test "
            "files. Do not read this as 'nothing to run'." % (len(terms), len(all_tests)),
            file=sys.stderr,
        )
        return 2

    covered_terms = set()
    for hits in selected.values():
        covered_terms |= hits
    uncovered = sorted(identifiers - covered_terms)

    elapsed = time.perf_counter() - started
    if arguments.format == "json":
        print(json.dumps({
            "schema": "sonder.incremental-test-selection.v1",
            "selected": sorted(selected),
            "selected_count": len(selected),
            "test_file_count": len(all_tests),
            "changed_modules": sorted(modules),
            "changed_identifiers": sorted(identifiers),
            "directly_changed_tests": sorted(direct_tests),
            "uncovered_identifiers": uncovered,
            "fallback_to_module": fallback,
            "elapsed_seconds": round(elapsed, 6),
        }, indent=2, sort_keys=True))
    elif arguments.format == "args":
        print(" ".join(sorted(selected)))
    else:
        for path in sorted(selected):
            print(path)

    print(
        "\n# selected %d of %d test files from %d changed identifier(s) "
        "across %d module(s) in %.3fs: %s%s"
        % (len(selected), len(all_tests), len(identifiers), len(modules),
           elapsed,
           ", ".join(sorted(modules)),
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
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
