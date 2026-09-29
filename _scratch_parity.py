"""Scratch: promotion decision matrix digest (branch vs main parity). Deleted before commit."""
import hashlib
import json

from sonder_runtime.application.evaluation.promotion_gates import (
    DEFAULT_PROMOTION_GATE_POLICIES, PromotionKind, evaluate_promotion_gate,
)
from sonder_runtime.application.evaluation.proposal_lifecycle import (
    EvaluationDimension, EvaluationMode, EvaluationResult, EvaluationSuite, ShadowCanaryObservation,
)

SUITE = EvaluationSuite("prompt-quality", "v1", (EvaluationDimension("split", "holdout"),), ("pass_rate",))
SHADOW = ShadowCanaryObservation(EvaluationMode.SHADOW, "s1", True, 20, {"error": 0}, 0)
CANARY = ShadowCanaryObservation(EvaluationMode.CANARY, "c1", True, 20, {"error": 0}, 0.05)
CANARY_BAD = ShadowCanaryObservation(EvaluationMode.CANARY, "c2", False, 3, {"error": 2}, 0.5)


def res(rid, passed, total, replay=True, prov=("test",), mode=EvaluationMode.OFFLINE):
    return EvaluationResult(rid, SUITE, "candidate", "baseline", mode, SUITE.dimensions,
                            {"pass_rate": passed / total}, passed == total, total,
                            replay_equivalent=replay, provenance=prov)


SCEN = {
    "pass": dict(results=[res("r1", 40, 40)]),
    "thin": dict(results=[res("r1", 3, 3)]),
    "dropped": dict(results=[res("r1", 30, 40)]),
    "nonreplay": dict(results=[res("r1", 40, 40, replay=False)]),
    "regressed": dict(results=[res("r1", 40, 40)], case_regressions=2),
    "no_canary": dict(results=[res("r1", 40, 40)], canary=None),
    "canary_only": dict(results=[], canary=CANARY),
    "no_baseline": dict(results=[res("r1", 40, 40)], baseline_pass_rate=None),
    "extra_prov": dict(results=[res("r1", 40, 40, prov=("test", "ci:abc", "evaluation:clean-note"))]),
    "bad_canary": dict(results=[res("r1", 40, 40)], canary=CANARY_BAD),
    "two_results": dict(results=[res("r1", 40, 40), res("r2", 38, 40)]),
}

rows = []
for kind in PromotionKind:
    if kind is PromotionKind.STRATEGY or kind not in DEFAULT_PROMOTION_GATE_POLICIES:
        continue
    policy = DEFAULT_PROMOTION_GATE_POLICIES[kind]
    for name, spec in SCEN.items():
        kwargs = dict(baseline_pass_rate=1.0, shadow=SHADOW, canary=CANARY)
        kwargs.update({k: v for k, v in spec.items()})
        try:
            d = evaluate_promotion_gate(policy, **kwargs)
            out = [d.digest, d.passed, list(d.reason_codes), dict(d.gate_results)]
        except Exception as e:  # noqa: BLE001
            out = ["error", type(e).__name__, str(e)]
        rows.append([kind.value, name, out])
blob = json.dumps(rows, sort_keys=True)
print("scenarios", len(rows))
print("matrix_sha256", hashlib.sha256(blob.encode()).hexdigest())
for r in rows:
    print(r[0], r[1], hashlib.sha256(json.dumps(r[2], sort_keys=True).encode()).hexdigest()[:16])
