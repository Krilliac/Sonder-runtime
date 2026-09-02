"""Outcome vocabulary, trial aggregation and run comparison for the eval harness.

Pure and dependency-free: no clock, no I/O, no model. ``eval_harness`` (the
operator CLI at the repository root) consumes these; ``reproducible`` is the
sibling lane whose ``RegressionAssessment`` the comparison reuses, so the two
lanes converge on one record type instead of growing a second.

The vocabulary keeps the never-merged outcome discipline of
``scripts/benchmark_schema_offload.py``: a graded status (``pass``/``fail``)
is a statement about the artifact under test; every other status is a
statement about the harness, the provider or the verifier, and is never
averaged into a pass rate.
"""
from __future__ import annotations

from typing import Any, Iterable, Mapping, Sequence

from .reproducible import RegressionAssessment

PASS = "pass"
FAIL = "fail"
# A cassette miss, or a provider that failed on every attempt.
ERROR = "error"
# The wall clock ran out before any attempt finished.
TIMEOUT = "timeout"
# The named verifier could not judge (its tool is absent), which is not a
# verdict about the artifact.
VERIFIER_UNAVAILABLE = "verifier_unavailable"
# The harness itself crashed between "started" and "graded".
UNKNOWN = "unknown"
# A live (non-deterministic) provider ran out of wall clock after at least one
# attempt: the case was started in earnest and then given up on.
ABANDONED = "abandoned"

STATUSES = (PASS, FAIL, ERROR, TIMEOUT, VERIFIER_UNAVAILABLE, UNKNOWN, ABANDONED)
GRADED = frozenset({PASS, FAIL})
INFRASTRUCTURE = frozenset(STATUSES) - GRADED

SAME, REGRESSED, IMPROVED, INFRA = "same", "regressed", "improved", "infra"
VERDICTS = (SAME, REGRESSED, IMPROVED, INFRA)
COMPARISON_SCHEMA = "sonder.eval-harness.comparison/v1"

# Reason codes a comparison can carry, in the order they are reported.
SUITE_MISMATCH = "suite_mismatch"
CASE_SET_MISMATCH = "case_set_mismatch"
CASE_REGRESSIONS = "case_regressions"
PASS_RATE_DROP = "pass_rate_drop"
INFRASTRUCTURE_OUTCOMES = "infrastructure_outcomes"
TRAJECTORY_DIVERGENCE = "trajectory_divergence"
# Only these fail a comparison; the others are named so a reader sees them.
BLOCKING_REASONS = frozenset({SUITE_MISMATCH, CASE_SET_MISMATCH, CASE_REGRESSIONS})


def validate_status(status: Any) -> str:
    if status not in STATUSES:
        raise ValueError("unknown outcome status %r (known: %s)"
                         % (status, ", ".join(STATUSES)))
    return status


def is_graded(status: str) -> bool:
    return validate_status(status) in GRADED


def is_infrastructure(status: str) -> bool:
    return validate_status(status) in INFRASTRUCTURE


def totals(statuses: Iterable[str]) -> dict:
    """Count every status; ``pass_rate`` is over graded cases only.

    Infrastructure statuses are counted individually and summed under
    ``infra``; they are reported beside the pass rate, never inside it.
    """
    counts = {"cases": 0}
    counts.update({status: 0 for status in STATUSES})
    for status in statuses:
        counts[validate_status(status)] += 1
        counts["cases"] += 1
    graded = counts[PASS] + counts[FAIL]
    counts["graded"] = graded
    counts["infra"] = counts["cases"] - graded
    counts["pass_rate"] = (counts[PASS] / graded) if graded else None
    return counts


def aggregate_trials(statuses: Sequence[str]) -> dict:
    """Fold ``k`` trials of one case into pass@1 and pass@k.

    The case's own status is the first trial's, so a ratchet that pins
    ``required_pass`` stays a statement about a single honest run; ``pass_at_k``
    is reported beside it, never instead of it.
    """
    if not statuses:
        raise ValueError("at least one trial is required")
    trials = [validate_status(status) for status in statuses]
    passes = sum(1 for status in trials if status == PASS)
    return {
        "status": trials[0],
        "trials": trials,
        "k": len(trials),
        "passes": passes,
        "pass_at_1": trials[0] == PASS,
        "pass_at_k": passes > 0,
    }


def classify_pair(before: str, after: str) -> str:
    """``same`` / ``regressed`` / ``improved`` / ``infra`` for one case."""
    before = validate_status(before)
    after = validate_status(after)
    if before in INFRASTRUCTURE or after in INFRASTRUCTURE:
        return INFRA
    if before == after:
        return SAME
    return REGRESSED if before == PASS else IMPROVED


def _is_digest(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _trajectory_verdict(before: Mapping[str, Any], after: Mapping[str, Any],
                        divergences) -> str:
    if divergences is not None:
        return "divergent" if divergences else "same"
    left = before.get("trajectory_digest")
    right = after.get("trajectory_digest")
    if not left or not right:
        return "unknown"
    return "same" if left == right else "divergent"


def compare_cases(before: Mapping[str, Mapping[str, Any]],
                  after: Mapping[str, Mapping[str, Any]], *,
                  before_run_id: str = "", after_run_id: str = "",
                  before_suite_hash: str = "", after_suite_hash: str = "",
                  before_pass_rate: float | None = None,
                  trajectory_divergences: Mapping[str, Sequence[Any]] | None = None) -> dict:
    """Join two runs by scenario id and classify every case.

    ``before``/``after`` map scenario id to a case row carrying ``status`` and,
    optionally, ``trajectory_digest``. ``trajectory_divergences`` may supply a
    step-level answer per scenario (an empty sequence means "identical") from
    the trajectory comparator; where it is absent the digests decide.

    A comparison *fails* only for a regression, a suite mismatch or a case-set
    mismatch. Infrastructure outcomes, a pass-rate drop that no single case
    explains, and trajectory divergence are named as reason codes so a reader
    sees them, but two honest runs of a non-deterministic provider differ in
    those ways without either being wrong.
    """
    reasons: list[str] = []
    if before_suite_hash != after_suite_hash:
        reasons.append(SUITE_MISMATCH)
    ids_before, ids_after = set(before), set(after)
    if ids_before != ids_after:
        reasons.append(CASE_SET_MISMATCH)
    divergences = trajectory_divergences or {}
    cases = []
    for scenario_id in sorted(ids_before & ids_after):
        left, right = before[scenario_id], after[scenario_id]
        divergence = divergences.get(scenario_id)
        cases.append({
            "scenario": scenario_id,
            "before": validate_status(left["status"]),
            "after": validate_status(right["status"]),
            "verdict": classify_pair(left["status"], right["status"]),
            "trajectory": _trajectory_verdict(left, right, divergence),
            "divergences": [dict(item) for item in divergence] if divergence else [],
        })
    regressed = sorted(c["scenario"] for c in cases if c["verdict"] == REGRESSED)
    improved = sorted(c["scenario"] for c in cases if c["verdict"] == IMPROVED)
    infra = sorted(c["scenario"] for c in cases if c["verdict"] == INFRA)
    if regressed:
        reasons.append(CASE_REGRESSIONS)
    after_totals = totals(row["status"] for row in after.values())
    after_rate = after_totals["pass_rate"]
    if (before_pass_rate is not None and after_rate is not None
            and after_rate < before_pass_rate):
        reasons.append(PASS_RATE_DROP)
    if infra:
        reasons.append(INFRASTRUCTURE_OUTCOMES)
    if any(c["trajectory"] == "divergent" for c in cases):
        reasons.append(TRAJECTORY_DIVERGENCE)
    passed = not (set(reasons) & BLOCKING_REASONS)

    baseline_id, baseline_rate = "", None
    if _is_digest(before_run_id) and before_pass_rate is not None:
        baseline_id, baseline_rate = before_run_id, before_pass_rate
    assessment = RegressionAssessment(
        passed, tuple(reasons), tuple(regressed), baseline_id, baseline_rate,
    )
    return {
        "schema": COMPARISON_SCHEMA,
        "passed": passed,
        "reason_codes": list(reasons),
        "regressed": regressed,
        "improved": improved,
        "infra": infra,
        "cases": cases,
        "before_run_id": before_run_id or None,
        "after_run_id": after_run_id or None,
        "before_pass_rate": before_pass_rate,
        "after_pass_rate": after_rate,
        "assessment": assessment.as_dict(),
    }


__all__ = [
    "ABANDONED", "BLOCKING_REASONS", "CASE_REGRESSIONS", "CASE_SET_MISMATCH",
    "COMPARISON_SCHEMA", "ERROR", "FAIL", "GRADED", "IMPROVED", "INFRA",
    "INFRASTRUCTURE", "INFRASTRUCTURE_OUTCOMES", "PASS", "PASS_RATE_DROP",
    "REGRESSED", "SAME", "STATUSES", "SUITE_MISMATCH", "TIMEOUT",
    "TRAJECTORY_DIVERGENCE", "UNKNOWN", "VERDICTS", "VERIFIER_UNAVAILABLE",
    "aggregate_trials", "classify_pair", "compare_cases", "is_graded",
    "is_infrastructure", "totals", "validate_status",
]
