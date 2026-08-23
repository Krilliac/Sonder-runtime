"""Verify relative Markdown links in operator/developer docs resolve to a file.

Scope is deliberately narrow: the top-level operator docs (README, SELFMOD,
SECURITY, ...), the wiki, and the runbooks. ``docs/architecture/`` is excluded
on purpose -- it is a generated/historical ledger maintained by
``generate_documentation_catalogs.py`` and audited separately by
``check_documentation_authority.py``.

Checks only that the file-path portion of a relative link exists; it does not
follow http(s)/mailto links (network access is out of scope for CI) and does
not validate ``#anchor`` fragments (GitHub's heading-slug rules are not worth
reimplementing for the handful of anchor links this doc set has).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

DOC_ROOTS = (
    REPO_ROOT,  # non-recursive: only *.md directly under the repo root
    REPO_ROOT / "docs" / "wiki",
    REPO_ROOT / "docs" / "runbooks",
    REPO_ROOT / "docs" / "security",
)

_LINK_RE = re.compile(r"\[[^\]\n]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SKIP_SCHEMES = ("http://", "https://", "mailto:", "ftp://")


def _doc_files():
    seen = []
    for root in DOC_ROOTS:
        if root == REPO_ROOT:
            files = sorted(root.glob("*.md"))
        else:
            files = sorted(root.rglob("*.md")) if root.is_dir() else []
        seen.extend(files)
    return seen


def check() -> list[str]:
    problems = []
    for doc in _doc_files():
        text = doc.read_text(encoding="utf-8", errors="replace")
        for match in _LINK_RE.finditer(text):
            target = match.group(1).strip()
            if not target or target.startswith(_SKIP_SCHEMES) or target.startswith("#"):
                continue
            if target.startswith("/"):
                # Repo-absolute links are not this checker's concern; the
                # doc sets involved only ever use relative paths.
                continue
            path_part = target.split("#", 1)[0]
            if not path_part:
                continue
            resolved = (doc.parent / path_part).resolve()
            if not resolved.exists():
                try:
                    relative_doc = doc.relative_to(REPO_ROOT)
                except ValueError:
                    relative_doc = doc
                problems.append(f"{relative_doc}: broken link -> {target}")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    if problems:
        print(f"\n{len(problems)} broken documentation link(s)", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
