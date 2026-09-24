# EVAL-007: per-kind promotion thresholds with confidence requirements

EVAL-007 requires thresholds and confidence requirements for runtime,
prompt, skill, route, model, memory, and selfmod promotion. Audit context:
[`EVAL-AUDIT-2026-09-23.md`](EVAL-AUDIT-2026-09-23.md). This follows issue
#510 section 5: the gate is a mechanical authority (arithmetic over
recorded counts), never a model's subjective pass/fail.

## What changed

- `sonder_runtime/application/evaluation/promotion_gates.py`
  - `PromotionKind` enumerates the seven promotion classes named by the
    requirement.
  - `PromotionGatePolicy` binds a kind to a minimum sample size, a point
    pass-rate floor, a one-sided Wilson score lower bound at a stated
    confidence level, a case-regression allowance, a pass-rate-drop allowance
    against a baseline, replay equivalence, and shadow/canary requirements
    (including a minimum canary sample count). Policies are digest-bound and
    reject incoherent settings (a confidence floor above the point floor, a
    canary without a shadow, out-of-range confidence).
  - `DEFAULT_PROMOTION_GATE_POLICIES` covers every kind; `validate_policy_table`
    refuses a table that omits or mislabels any kind.
  - `evaluate_promotion_gate` pools offline results, derives the integral
    success count from `pass_rate x sample_count` (refusing fractional counts
    or a missing `pass_rate` metric instead of rounding them into a pass), and
    returns a `PromotionGateDecision` with named sub-gate results, reason
    codes, and a stable digest. Results must have distinct `result_id`s and
    share one candidate, baseline, and suite digest, so duplicates cannot
    inflate the sample count and unrelated results cannot be pooled.
- `sonder_runtime/application/evaluation/service.py` binds the gate at the
  service boundary:
  - `create_proposal(..., kind=)` fixes the promotion kind when the proposal
    is created.
  - `promotion_gate_decision` and `gated_promotion_evidence` do not accept a
    decision object. They recompute it from the lifecycle-recorded results
    and shadow/canary observations (`ProposalLifecycle.recorded_results` /
    `recorded_observation`, also exposed by the durable lifecycle service and
    the port) under that kind's policy. Baseline inputs
    (`baseline_pass_rate`, `case_regressions`) must be stated explicitly.
  - The decision's sub-gates become `PromotionEvidence.gate_results` and its
    digest is added to provenance, so a failed sub-gate makes the evidence
    unacceptable.
  - For kind-bound proposals, the ungated `promotion_evidence` path is
    refused, and `approve` accepts only the digest of evidence the gated path
    produced (evidence built directly on the lifecycle is refused). Legacy
    proposals created without a kind keep the ungated path, which now emits
    `DeprecationWarning`.

## Evidence

`tests/test_eval007_promotion_gates.py` (15 tests, no model or live traffic):

- The Wilson bound matches the closed form for 10/10, is pinned at the
  interior rate 27/30 (0.77450, exercising the `p(1-p)` term), and separates
  3/3 (about 0.53) from 300/300 (above 0.98).
- Three copies of one 10/10 result are refused (before the fix they pooled to
  30 samples and passed the PROMPT gate); results for another candidate or
  another suite version are refused.
- A SELFMOD-bound proposal with 40/40 fails `sample_size` because the service
  applies the bound kind; a caller-built PROMPT decision cannot be passed in;
  lifecycle-built ungated evidence cannot be approved through the service.
- Every default policy's sample floor is attainable: a perfect run of
  `min_samples` clears its own confidence bound.
- A perfect 3/3 prompt run fails on `sample_size` and
  `confidence_lower_bound`; a pooled 39/40 run passes all eight named gates,
  independent of result order.
- Case regressions, pass-rate drop, missing replay equivalence, a missing or
  thin canary, an unhealthy shadow, and the absence of offline results each
  fail with a specific reason code.
- Through the application service, a gated-but-failing proposal cannot be
  approved; a passing one is approved and then promoted only with an attended
  decision.

Mutation check (local): forcing `confidence_lower_bound` to pass fails the
small-sample test, so the confidence assertion is load-bearing. Each review
fix was preceded by a test that failed on the previous code.

Local verification: the 15 tests, the pre-existing evaluation suites,
`scripts/check_architecture.py`, `scripts/check_requirement_evidence.py
--base-ref origin/main`, `scripts/check_evidence_documents.py`,
`scripts/check_doc_links.py`, and `git diff --check` passed.

## Limitations

- The requirement stays `implemented_unverified`: the existing selfmod,
  memory, and model promotion paths (`selfmod.py`, `promotion_eval.py`, and
  the nightly scripts) do not yet call this gate, so the policy table is
  defined and enforced for proposals that go through the evaluation service,
  not yet for every production promotion.
- The default thresholds are conservative starting values, not calibrated
  against live workload variance.
- Pooling assumes offline results for one proposal are independent samples of
  the same suite; the gate does not model correlated cases.
- The kind binding and gated-evidence record live in the service instance;
  they are not persisted with the durable lifecycle events.
- `baseline_pass_rate` and `case_regressions` are still caller-stated inputs.
- `evaluate_promotion_gate` and `PromotionGateDecision` remain public for
  analysis, but the service never accepts a caller-built decision.
