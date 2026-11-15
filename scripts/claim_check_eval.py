#!/usr/bin/env python3
"""Measure the claim checker against a labelled JSONL of model claims.

Each row needs ``file``, the claim prose (default field ``hypothesis``), a
label (default field ``label``) and a repository root (``repo_root`` or
``--root``). Citations are taken from the prose the way ``claim_from_mapping``
does. Only aggregate counts are printed: no row text, path or id leaves this
script, so the labelled set can stay local.

    python scripts/claim_check_eval.py rows.jsonl [--root DIR] [--symbols all|any|off]
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sonder_runtime.adapters.claim_checker import ClaimChecker, claim_from_mapping  # noqa: E402
from sonder_runtime.domain.claim_check import ClaimError  # noqa: E402
from sonder_runtime.platform.claim_check_config import ClaimCheckConfig  # noqa: E402


def evaluate(path, root=None, *, text_field="hypothesis", label_field="label",
             symbols="all", config=None, use_symbols=True) -> dict:
    config = config or ClaimCheckConfig()
    if symbols in ("all", "any"):
        config = replace(config, symbol_match=symbols)
    rows = []
    with open(path, encoding="utf-8") as handle:
        for raw in handle:
            if raw.strip():
                rows.append(json.loads(raw))
    checkers: dict[str, ClaimChecker] = {}
    table: Counter = Counter()
    by_check: Counter = Counter()
    invalid = 0
    for row in rows:
        mapping = {"file": row.get("file"), "text": row.get(text_field, ""),
                   "category": row.get("category", "")}
        try:
            claim = claim_from_mapping(mapping)
        except ClaimError:
            invalid += 1
            continue
        if symbols == "off" or not use_symbols:
            claim = replace(claim, symbols=())
        base = str(root or row.get("repo_root") or "")
        checker = checkers.get(base)
        if checker is None:
            checker = checkers[base] = ClaimChecker(base, config)
        report = checker.check(claim)
        label = str(row.get(label_field, "?"))
        table[(label, report.verdict.value)] += 1
        for check in report.checks:
            by_check[(label, check.name, check.status.value)] += 1
    return {"rows": len(rows), "invalid": invalid, "table": table, "by_check": by_check}


def render(result: dict) -> str:
    labels = sorted({label for label, _ in result["table"]})
    lines = [f"rows={result['rows']} invalid_claims={result['invalid']}",
             "label        total  reject   flag   keep"]
    for label in labels:
        counts = {v: result["table"][(label, v)] for v in ("reject", "flag", "keep")}
        lines.append(f"{label:<12} {sum(counts.values()):>5} {counts['reject']:>7} "
                     f"{counts['flag']:>6} {counts['keep']:>6}")
    lines.append("")
    lines.append("check outcomes by label (fail / pass / not_checkable)")
    names = sorted({name for _, name, _ in result["by_check"]})
    for label in labels:
        for name in names:
            triple = [result["by_check"][(label, name, s)] for s in ("fail", "pass", "not_checkable")]
            if any(triple):
                lines.append(f"  {label:<10} {name:<14} {triple[0]:>5} {triple[1]:>5} {triple[2]:>5}")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("rows")
    parser.add_argument("--root", default=None)
    parser.add_argument("--text-field", default="hypothesis")
    parser.add_argument("--label-field", default="label")
    parser.add_argument("--symbols", choices=("all", "any", "off"), default="all")
    parser.add_argument("--set", action="append", default=[], metavar="NAME=INT",
                        help="override an integer [claim_check] setting, e.g. guard_window_lines=5")
    args = parser.parse_args(argv)
    config = ClaimCheckConfig()
    for item in args.set:
        name, _, value = item.partition("=")
        if not hasattr(config, name) or not value.lstrip("-").isdigit():
            parser.error(f"--set needs an integer [claim_check] field, got {item!r}")
        config = replace(config, **{name: int(value)})
    result = evaluate(args.rows, args.root, text_field=args.text_field,
                      label_field=args.label_field, symbols=args.symbols, config=config)
    print(render(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
