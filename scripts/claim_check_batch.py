#!/usr/bin/env python3
"""Check a JSONL of model claims against a repository, read-only.

Each input row is a claim object (``file``, ``line`` or ``lines``, ``symbols``,
``category``, ``guard``, ``entry_point``, ``id``, optional ``text`` prose and
``repo_root``). One JSON line per input row is written to the output file, in
order: a report with the verdict, or a typed ``error`` for a malformed row.

    python scripts/claim_check_batch.py claims.jsonl report.jsonl --root path/to/repo
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sonder_runtime.adapters.claim_checker import check_jsonl  # noqa: E402
from sonder_runtime.domain.claim_check import ClaimError  # noqa: E402
from sonder_runtime.platform.claim_check_config import ClaimCheckConfig  # noqa: E402


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("claims")
    parser.add_argument("report")
    parser.add_argument("--root", required=True, help="default repository root for rows without repo_root")
    args = parser.parse_args(argv)
    if not os.path.isdir(args.root):
        print(f"error: --root {args.root!r} is not a directory", file=sys.stderr)
        return 2
    try:
        summary = check_jsonl(args.claims, args.report, args.root, ClaimCheckConfig())
    except (ClaimError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    verdicts = summary.verdicts
    print(f"total={summary.total} errors={summary.errors} duplicates={summary.duplicates} "
          f"reject={verdicts['reject']} flag={verdicts['flag']} keep={verdicts['keep']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
