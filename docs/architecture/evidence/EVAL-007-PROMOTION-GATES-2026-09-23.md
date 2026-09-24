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
    against a baseline, a `require_baseline` flag, replay equivalence, and
    shadow/canary requirements (including a minimum canary sample count).
    Policies are digest-bound and reject incoherent settings.
  - `DEFAULT_PROMOTION_GATE_POLICIES` covers every kind; `validate_policy_table`
    refuses a table that omits or mislabels any kind. Every kind except SKILL
    (a new skill may have no predecessor) requires an explicit baseline:
    `baseline_pass_rate=None` fails the `baseline_comparison` gate instead of
    silently skipping the pass-rate-drop check.
  - `evaluate_promotion_gate` pools offline results, derives the integral
    success count from `pass_rate x sample_count` (refusing fractional counts
    or a missing `pass_rate` metric), and returns a `PromotionGateDecision`
    with named sub-gate results, reason codes, and a stable digest. Results
    must have distinct `result_id`s and share one candidate, baseline, and
    suite digest.
  - `PromotionGateEvaluator` freezes a validated policy table for use as the
    lifecycle's gate.
- `sonder_runtime/application/evaluation/proposal_lifecycle.py` owns the gate
  authority, so it cannot be bypassed by constructing another service:
  - `ProposalLifecycle(promotion_gate=...)` fixes the gate at construction.
  - `create(..., promotion_kind=)` stores the kind on the proposal itself.
  - `build_gated_promotion_evidence` runs the construction-time gate over the
    results and observations this lifecycle recorded, and records the
    evidence and decision digests as gated.
  - For kind-bound proposals, caller-asserted `build_promotion_evidence` is
    refused and `approve` accepts only gated evidence; the legacy opt-in is
    refused too.
  - For proposals without a kind, `approve` refuses ungated evidence unless
    the caller passes `allow_ungated_legacy=True`. The only existing callers of
    that path are the three legacy-contract test modules, which now opt in
    explicitly; there are no production callers.
- `sonder_runtime/application/evaluation/durable_lifecycle.py` persists the
  authority in the hash-chained lifecycle events: the creation event carries
  `promotion_kind`, evidence events carry `gated` and `gate_decision_digest`,
  and the approval event records whether it was gated.
- `sonder_runtime/application/evaluation/service.py` delegates to the
  lifecycle (`create_proposal(..., kind=)`, `promotion_gate_decision`,
  `gated_promotion_evidence`, `approve(..., allow_ungated_legacy=)`); it holds
  no gate state and accepts no decision object. The ungated
  `promotion_evidence` path emits `DeprecationWarning` when the lifecycle
  accepts it.

## Evidence

`tests/test_eval007_promotion_gates.py` (19 tests, no model or live traffic):

- The Wilson bound matches the closed form for 10/10, is pinned at the
  interior rate 27/30 (0.77450), and separates 3/3 from 300/300.
- Duplicate result IDs and results for another candidate or suite version are
  refused.
- A second service over the same lifecycle cannot take the ungated path, and
  lifecycle-built ungated evidence is refused, so a 3/3 SELFMOD proposal stays
  in `canary`. Before the fix, a second service moved it to
  `ready_for_promotion`.
- Proposals without a kind need the explicit legacy opt-in to be approved.
- Kind, gated flag, and decision digest are read back from the SQLite event
  history after reopening; a restarted lifecycle has no proposal state and
  refuses approval.
- `baseline_pass_rate=None` fails `baseline_comparison` for PROMPT and passes
  for SKILL. Before the fix, a 40/40 PROMPT run with no baseline passed.
- A SELFMOD-bound proposal with 40/40 fails `sample_size`; a caller-built
  PROMPT decision cannot be passed in.
- Default sample floors are attainable; small or thin runs, regressions,
  pass-rate drops, missing replay equivalence, and missing or unhealthy
  shadow/canary observations each fail with a specific reason code.

Each review fix was preceded by a test or reproduction that failed on the
previous code.

## Limitations

- The requirement stays `implemented_unverified`: the existing selfmod,
  memory, and model promotion paths (`selfmod.py`, `promotion_eval.py`, and
  the nightly scripts) do not yet call this gate.
- The durable events record the kind and gate decisions, but the in-memory
  `ProposalLifecycle` is not rehydrated from them on restart; a restarted
  lifecycle fails closed (unknown proposal) rather than resuming.
- The default thresholds are conservative starting values, not calibrated
  against live workload variance.
- Pooling assumes offline results for one proposal are independent samples of
  the same suite; the gate does not model correlated cases.
- `baseline_pass_rate` and `case_regressions` are caller-stated inputs, though
  they must be stated explicitly.
