#!/usr/bin/env python3
"""Validate an evaluation-case manifest and preflight local verifier support."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from sonder_runtime.application.evaluation.case_manifest import (  # noqa: E402
    EvaluationCaseManifestError,
    inspect_manifest,
    load_manifest,
)
import verifiers  # noqa: E402


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate a bounded local evaluation-case manifest without executing it.",
    )
    parser.add_argument("manifest", help="path to a local JSON manifest")
    parser.add_argument("--json", action="store_true", help="emit machine-readable diagnostics")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        diagnostics = inspect_manifest(manifest, verifiers.REGISTRY)
    except EvaluationCaseManifestError as exc:
        if args.json:
            print(json.dumps({
                "schema": "sonder.evaluation-case-diagnostics.v1",
                "valid": False,
                "error": str(exc),
            }, sort_keys=True))
        else:
            print(f"INVALID: {exc}", file=sys.stderr)
        return 2

    payload = {"valid": True, **diagnostics.as_dict()}
    if args.json:
        print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    else:
        state = "READY" if diagnostics.runnable and diagnostics.gate_ready else "NOT READY"
        print(f"{state}: {diagnostics.case_count} cases; digest={diagnostics.manifest_digest}")
        print(f"deterministic={diagnostics.deterministic_cases} advisory={diagnostics.advisory_cases}")
        for warning in diagnostics.warnings:
            print(f"warning: {warning}")
    return 0 if diagnostics.runnable and diagnostics.gate_ready else 1


if __name__ == "__main__":
    raise SystemExit(main())
