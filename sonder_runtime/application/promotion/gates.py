"""Evaluate measured promotion evidence without mutating active state."""
from __future__ import annotations

import hashlib
import json
from typing import Mapping

from sonder_runtime.domain.promotion.measured import (
    MeasuredEvidence, PromotionArea, PromotionDecision, PromotionPolicy,
)


class MeasuredPromotionGates:
    """Pure evaluator for skills, routing, memory, models, and self-mod.

    This service returns an authorization-shaped decision only.  Applying a
    promotion and executing rollback remain outside the evaluator.
    """

    def __init__(self, policies: Mapping[PromotionArea, PromotionPolicy]) -> None:
        self._policies = dict(policies)

    def evaluate(self, evidence: MeasuredEvidence) -> PromotionDecision:
        policy = self._policies.get(evidence.area)
        digest = self._digest(evidence)
        if policy is None:
            return self._reject(evidence, digest, "no_policy", ("policy",))
        failures: list[str] = []
        if policy.require_holdout and not evidence.holdout_passed:
            failures.append("holdout")
        missing = sorted(set(policy.required_provenance) - set(evidence.provenance))
        failures.extend(f"provenance:{item}" for item in missing)
        if policy.require_rollback and not evidence.rollback_reference.strip():
            failures.append("rollback")
        for metric, minimum in policy.minimums.items():
            if metric not in evidence.metrics:
                failures.append(f"metric_missing:{metric}")
            elif evidence.metrics[metric] < minimum:
                failures.append(f"metric_below:{metric}")
        for metric, max_regression in policy.maximum_regressions.items():
            if metric in evidence.metrics and evidence.metrics[metric] < -max_regression:
                failures.append(f"regression:{metric}")
        if failures:
            return self._reject(evidence, digest, "promotion_rejected", tuple(failures))
        return PromotionDecision(
            True, "promotion_approved", evidence.area, evidence.candidate,
            rollback_reference=evidence.rollback_reference, evidence_digest=digest,
        )

    @staticmethod
    def _digest(evidence: MeasuredEvidence) -> str:
        payload = {
            "area": evidence.area.value, "candidate": evidence.candidate,
            "baseline": evidence.baseline, "metrics": dict(evidence.metrics),
            "holdout_passed": evidence.holdout_passed,
            "provenance": evidence.provenance,
            "rollback_reference": evidence.rollback_reference,
        }
        return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    @staticmethod
    def _reject(evidence, digest, reason, failures):
        return PromotionDecision(False, reason, evidence.area, evidence.candidate, failures, evidence_digest=digest)


__all__ = ["MeasuredPromotionGates"]
