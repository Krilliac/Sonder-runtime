"""Check evidence-document links and limitation disclosures deterministically."""
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs" / "architecture"
TEST_REF = re.compile(r"`(tests/[^`]+\.py)`")


def check() -> list[str]:
    problems: list[str] = []
    for path in sorted(DOCS.glob("REMAINING-*.md")):
        text = path.read_text(encoding="utf-8")
        if not re.search(r"(?i)\b(?:limitation|evidence|verification|coverage)\b", text):
            problems.append(f"{path.relative_to(ROOT)} lacks an evidence/limitation disclosure")
        refs = TEST_REF.findall(text)
        if not refs:
            problems.append(f"{path.relative_to(ROOT)} names no focused test")
        for ref in refs:
            if not (ROOT / ref).is_file():
                problems.append(f"{path.relative_to(ROOT)} references missing {ref}")
    return problems


def main() -> int:
    problems = check()
    for problem in problems:
        print(problem)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
