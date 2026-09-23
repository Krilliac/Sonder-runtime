# EVAL-001 through EVAL-009 audit — 2026-09-23

Scope: the nine evaluation requirements in section 11 of
[`SONDER-MASTER-IMPLEMENTATION-SPEC.md`](../SONDER-MASTER-IMPLEMENTATION-SPEC.md),
audited against `main` at `494f2397601b784abe82d5131693f16baad709d4`. At that
commit every EVAL ledger record's latest revision is
`implemented_unverified` (revision 2) and no EVAL checkbox is ticked. The
audit also applies issue #510 section 5 ("prefer deterministic gates —
tests/build/lint/evals — over subjective model pass/fail").

Method: read each implementation path named by the ledger plus the
surrounding evaluation package, located every test that imports it, and ran
the focused suites locally (Windows, Python 3.12, `requirements-dev.txt`
virtualenv): 69 tests passed across `tests/test_eval002_corpus_inventory.py`,
`tests/test_eval009_durable_lifecycle.py`,
`tests/test_evaluation_application_boundary.py`,
`tests/test_evaluation_case_manifest.py`,
`tests/test_evaluation_history_service.py`,
`tests/test_eval_harness_outcomes.py`,
`tests/test_remaining_evaluation_lifecycle.py`,
`tests/test_reproducible_evaluation.py`, and
`tests/test_wp6_trajectory_replay.py`. No live model or GPU was available;
nothing below claims a live-model result.

Verdict key: **met** — the requirement text is fully implemented and tested;
**partial** — a real, tested slice exists but part of the requirement text
has no implementation; **gap** — the named capability is absent.

## Summary

| Requirement | Verdict before this change | Principal gap |
|---|---|---|
| EVAL-001 First-class domain | partial | `EvaluationApplicationService` is not composed in the bootstrap graph or reachable from any transport. |
| EVAL-002 Suites | partial | No continuation or recovery suite; repository/tool/memory inventory has no production source reader. |
| EVAL-003 Metrics | partial | Cost, tool calls, retries, and resource use are not measured. |
| EVAL-004 Dimensions | gap | Only provider/model/revision/digest are bound; route, prompt manifest, skill/tool catalog, runtime version, hardware, and environment are not. |
| EVAL-005 Trajectory replay | partial | Replay takes an arbitrary evaluator; no recorded-side-effect substitution for real sessions. |
| EVAL-006 Divergence | partial → **closed here** | No "meaningful" filter and no minimization or retention of failures. |
| EVAL-007 Promotion gates | partial → **narrowed here** | Point-estimate thresholds only; no confidence requirement and no per-kind policy. |
| EVAL-008 Shadow/canary | partial | Observations are recorded; nothing routes live traffic or demotes automatically. |
| EVAL-009 Proposal lifecycle | partial | States differ from the spec vocabulary (no accepted/implementing/experimental/superseded); no owner or exit-criteria fields. |

## Per-requirement findings

### EVAL-001 — First-class evaluation domain

- Implementation: `sonder_runtime/application/evaluation/` (`service.py`,
  `proposal_lifecycle.py`, `reproducible.py`, `trajectory_replay.py`,
  `case_manifest.py`, `corpus_inventory.py`, `durable_lifecycle.py`,
  `harness_outcomes.py`), ports in
  `sonder_runtime/application/ports/evaluation.py`, adapters
  `sonder_runtime/adapters/reproducible_evaluation.py`,
  `evaluation_corpus.py`, `evaluation_lifecycle.py`,
  `evaluation_history_store.py`.
- Immutable run records: `EvaluationRunReport`, `EvaluationMatrixReport`,
  `TrajectoryRecord`, and `EvaluationResult` are frozen, digest-bound, and
  refuse tampered payloads on load (`tests/test_reproducible_evaluation.py`,
  `tests/test_wp6_trajectory_replay.py`).
- Tests: `tests/test_evaluation_application_boundary.py` (3 tests).
- Verdict: partial. Gap: `EvaluationApplicationService` has no production
  composition — `sonder_runtime/bootstrap/` composes only the separate
  `EvaluationHistoryService`. The operator path is the root-level
  `eval_harness.py` CLI, which does not go through the service.
  Closing it requires a bootstrap edit (`sonder_runtime/bootstrap/app.py` is
  in an open pull request's diff, so this lane did not touch it).

### EVAL-002 — Suites

- Implementation: `eval_scenarios/` holds `smoke_python`,
  `tool_policy_gates`, and `adversarial_safety` suites plus a replay cassette;
  `eval_harness.py` adapts `training_tasks.TASKS` (fixed tasks);
  `corpus_inventory.py` requires repository, tool, and memory source classes.
- Tests: `tests/test_eval002_corpus_inventory.py`,
  `tests/test_evaluation_case_manifest.py`, `tests/test_eval_harness_outcomes.py`;
  CI runs the smoke and tool-policy lanes against a baseline ratchet.
- Verdict: partial. Fixed tasks, tool use, and permission attacks exist as
  runnable suites; `grounding` is used only as the harness code-execution grader, not as a grounding suite. There is no
  continuation suite and no recovery suite, and the corpus inventory's
  production source readers are not deployed (the adapter reports them
  incomplete by design).

### EVAL-003 — Metrics

- Implementation: `CaseOutcome` records correctness (status/digests),
  latency, and input/output tokens; `EvaluationRunReport` derives pass,
  timeout, and error rates; `RegressionAssessment` names regressed cases.
- Tests: `tests/test_reproducible_evaluation.py`, `tests/test_eval_harness_outcomes.py`.
- Verdict: partial. Correctness, regressions, latency, and tokens are
  measured. Cost, tool-call count, retries, and resource use (CPU, memory,
  wall time beyond per-case latency) are not recorded anywhere in the
  evaluation records.

### EVAL-004 — Dimensions

- Implementation: `ProviderIdentity` binds provider, model, revision, and a
  provider digest; `EvaluationDimension` is a generic name/value pair; a
  scenario digest is bound as the `fixture` dimension.
- Tests: `tests/test_reproducible_evaluation.py`,
  `tests/test_remaining_evaluation_lifecycle.py`.
- Verdict: gap for most of the list. Route, prompt manifest, skill catalog,
  tool catalog, runtime version, hardware, and environment are not required
  or captured by any evaluation record. The earlier audit row
  (`REQUIREMENT-AUDIT-NEXT.md`, "PROVEN-CONTRACT") overstates this: the
  lifecycle tests bind a caller-chosen dimension tuple, not these identities.

### EVAL-005 — Trajectory replay

- Implementation: `trajectory_replay.py` (`replay_trajectory`,
  `compare_trajectories`), `ReproducibleEvaluationRunner.replay`, and the
  `eval_harness.py` replay cassette (a cassette miss is an infrastructure
  outcome, never a graded result).
- Tests: `tests/test_wp6_trajectory_replay.py`,
  `tests/test_reproducible_evaluation.py`.
- Verdict: partial. Deterministic replay with alternate evaluators works.
  Replaying *recorded sessions* while substituting recorded side effects
  (tool results, file writes) is not implemented: nothing intercepts a
  side-effecting tool call during replay and serves the recorded result.

### EVAL-006 — Divergence

- Before: `ReplayReport.divergences` listed every raw per-step output
  difference, so timing or ID noise was reported as a divergence, and nothing
  minimized or retained failing cases.
- Closed in this change: see
  [`EVAL-006-DIVERGENCE-MINIMIZATION-2026-09-23.md`](EVAL-006-DIVERGENCE-MINIMIZATION-2026-09-23.md).

### EVAL-007 — Promotion gates

- Before: `RegressionThresholds` gates one run on point estimates (a 3/3 run
  and a 300/300 run are indistinguishable); `PromotionEvidence` accepts any
  caller-supplied boolean `gate_results`; `promotion_eval.py` is a separate
  SQL-specific model gate.
- Narrowed in this change: see
  [`EVAL-007-PROMOTION-GATES-2026-09-23.md`](EVAL-007-PROMOTION-GATES-2026-09-23.md).
  Remaining gap: the production selfmod, memory, and model promotion paths do
  not yet call the new gate.

### EVAL-008 — Shadow/canary

- Implementation: `ShadowCanaryObservation` (shadow traffic must be zero,
  canary positive), healthy-shadow-before-canary transition,
  `PromotionEvidence.accepted` requiring healthy shadow and canary.
- Tests: `tests/test_remaining_evaluation_lifecycle.py`,
  `tests/test_evaluation_application_boundary.py`.
- Verdict: partial. The contract exists; no component runs a candidate on
  shadow traffic, selects canary-eligible work, or automatically demotes a
  measured regression (rollback is attended-only by design).

### EVAL-009 — Proposal lifecycle

- Implementation: `ProposalLifecycle` state machine, durable
  `EvaluationLifecycleService` with hash-chained events
  (`sonder_runtime/adapters/evaluation_lifecycle.py`).
- Tests: `tests/test_eval009_durable_lifecycle.py`,
  `tests/test_remaining_evaluation_lifecycle.py`.
- Verdict: partial. The implemented states are draft, submitted, evaluating,
  shadow, canary, ready-for-promotion, promoted, rejected, withdrawn, and
  rolled-back. The spec's accepted, implementing, experimental, and
  superseded states are absent, and proposals carry no owner or exit
  criteria.

## Gaps closed or narrowed in this change

1. **EVAL-006** — earliest *meaningful* divergence under an explicit
   decision/noise policy; prefix and differential-ddmin minimization with
   fresh evaluators per trial; deterministic reproduction check; immutable,
   digest-verified `MinimizedFailure` records; bounded in-memory and
   file-backed retention; service methods. Tests:
   `tests/test_eval006_divergence_minimization.py` (11).
2. **EVAL-007** — per-kind gate policies for runtime, prompt, skill, route,
   model, memory, and selfmod promotion with minimum samples, point floors,
   one-sided Wilson lower bounds at a stated confidence, regression and
   pass-rate-drop allowances, replay equivalence, and shadow/canary
   requirements; the service binds the mechanical decision into
   `PromotionEvidence`, so `approve` refuses a failed gate. Tests:
   `tests/test_eval007_promotion_gates.py` (8).

Both use deterministic in-process fakes only, which the requirement text
allows because neither capability depends on model behavior: divergence and
minimization are algorithms over recorded trajectories and the gates are
arithmetic over recorded counts.

## Recommended next slices

- EVAL-004: add a required `EvaluationEnvironment` binding (route, prompt
  manifest digest, skill/tool catalog digests, runtime version, hardware
  profile, environment digest) to `EvaluationRunReport` and refuse
  comparisons across mismatched bindings.
- EVAL-003: extend `CaseOutcome` with tool-call count, retries, cost, and
  resource counters; thread them through `eval_harness.py`.
- EVAL-001: compose `EvaluationApplicationService` in the bootstrap graph once
  the open bootstrap pull requests land.
- EVAL-007 follow-through: route selfmod and model promotion decisions through
  `EvaluationApplicationService.evaluate_promotion_gate`.
