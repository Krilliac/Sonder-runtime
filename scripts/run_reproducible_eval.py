"""Run a digest-bound golden scenario against deterministic local providers.

This command is intentionally offline.  Provider fixtures are explicit JSON
request/result tables; they validate the evaluation harness, replay, matrix,
and regression gates without contacting a model or network service.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sonder_runtime.adapters.reproducible_evaluation import (  # noqa: E402
    JsonEvaluationMatrixRepository,
    JsonEvaluationRunRepository,
    load_provider_fixture,
    load_scenario_fixture,
)
from sonder_runtime.application.evaluation.reproducible import (  # noqa: E402
    ReproducibleEvaluationError,
    ReproducibleEvaluationRunner,
    evaluation_diagnostics,
)


def _path_key(value: str | Path) -> str:
    return os.path.normcase(str(Path(value).expanduser().resolve()))


def _load_baseline(path: str):
    try:
        return JsonEvaluationRunRepository(path).load()
    except ReproducibleEvaluationError as run_error:
        try:
            matrix = JsonEvaluationMatrixRepository(path).load()
        except ReproducibleEvaluationError:
            raise run_error
        if len(matrix.runs) != 1:
            raise ReproducibleEvaluationError(
                "baseline matrix must contain exactly one run"
            )
        return matrix.runs[0]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", required=True, help="digest-bound scenario JSON")
    parser.add_argument("--provider", action="append", required=True, help="deterministic provider JSON; repeat for a matrix")
    parser.add_argument("--baseline", default="", help="optional prior run or single-target matrix JSON for regression thresholds")
    parser.add_argument("--output", required=True, help="atomic matrix report destination")
    parser.add_argument("--json-stdout", action="store_true", help="print the full replay-capable report")
    args = parser.parse_args(argv)
    inputs = [args.scenario, *args.provider]
    if args.baseline:
        inputs.append(args.baseline)
    if _path_key(args.output) in {_path_key(path) for path in inputs}:
        print("reproducible evaluation error: output must not overwrite an input fixture", file=sys.stderr)
        return 2
    try:
        scenario = load_scenario_fixture(args.scenario)
        providers = [load_provider_fixture(path) for path in args.provider]
        baseline = _load_baseline(args.baseline) if args.baseline else None
        report = ReproducibleEvaluationRunner().run_matrix(scenario, providers, baseline=baseline)
        JsonEvaluationMatrixRepository(args.output).save(report)
        if args.json_stdout:
            print(json.dumps(report.as_dict(), sort_keys=True, ensure_ascii=False))
        else:
            print(json.dumps({
                "matrix_id": report.digest,
                "scenario_digest": report.scenario_digest,
                "runs": [evaluation_diagnostics(run) for run in report.runs],
                "report": str(Path(args.output)),
                "privacy": "report contains raw replay inputs and outputs",
            }, sort_keys=True, ensure_ascii=False))
        return 0 if all(run.assessment.passed for run in report.runs) else 1
    except (OSError, ReproducibleEvaluationError, ValueError) as exc:
        print(f"reproducible evaluation error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
