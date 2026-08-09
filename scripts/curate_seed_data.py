#!/usr/bin/env python3
"""Check or repair the packaged lesson corpus's quality/provenance claims."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import reflection  # noqa: E402


def _atomic_write(path: Path, lines: list[str]) -> None:
    temp = path.with_name(path.name + ".curate.tmp")
    try:
        with temp.open("w", encoding="utf-8", newline="\n") as handle:
            handle.writelines(lines)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def inspect(*, fix: bool = False) -> dict[str, int]:
    lessons_path = REPO_ROOT / "lessons.jsonl"
    lesson_lines = lessons_path.read_text(encoding="utf-8").splitlines(keepends=True)
    kept: list[str] = []
    vague = 0
    for line in lesson_lines:
        if not line.strip():
            continue
        record = json.loads(line)
        text = (record.get("text") or record.get("lesson") or "").strip()
        if not text or reflection._looks_vague(text):
            vague += 1
            continue
        kept.append(line if line.endswith("\n") else line + "\n")

    false_grounded_claims = 0
    grounded_updates: list[tuple[Path, list[str]]] = []
    for path in sorted((REPO_ROOT / "seed" / "grounded").glob("*.jsonl")):
        lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
        updated = []
        for line in lines:
            record = json.loads(line)
            if record.get("grounded") is True:
                false_grounded_claims += 1
                line = line.replace('"grounded": true', '"grounded": false', 1)
            updated.append(line if line.endswith("\n") else line + "\n")
        grounded_updates.append((path, updated))

    if fix:
        _atomic_write(lessons_path, kept)
        for path, lines in grounded_updates:
            _atomic_write(path, lines)
    return {
        "vague_packaged_lessons": vague,
        "seed_grounded_true_claims": false_grounded_claims,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fix", action="store_true")
    args = parser.parse_args(argv)
    findings = inspect(fix=args.fix)
    print(json.dumps(findings, sort_keys=True))
    return 0 if args.fix or not any(findings.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
