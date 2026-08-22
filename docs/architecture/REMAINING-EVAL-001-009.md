# REMAINING-EVAL-001–009 — Evaluation proposal lifecycle

This slice closes the application-level lifecycle gap between deterministic
trajectory replay and measured promotion gates. `proposal_lifecycle.py` adds:

- first-class versioned `EvaluationSuite` identity and immutable named result
  dimensions;
- bounded, suite-bound `EvaluationResult` values carrying metrics, sample
  counts, replay identity, and provenance;
- explicit shadow and canary observations with health and traffic constraints;
- immutable `PromotionEvidence` combining result IDs, dimensions, gate outcomes,
  replay/holdout outcomes, shadow/canary health, provenance, and a rollback
  reference; and
- a fail-closed `ProposalLifecycle` state machine from draft through submitted,
  evaluation, shadow, canary, approval, promotion, rejection, withdrawal, and
  rollback states.

The lifecycle is intentionally an in-memory application reference. It performs
no model execution, persistence, deployment, network access, or automatic
promotion. Approval and promotion are separate explicit operations; promotion
requires an attended decision and the exact digest of accepted evidence.

Coverage is in `tests/test_remaining_evaluation_lifecycle.py`, with focused
tests for dimension identity, metric binding, shadow-to-canary health gating,
evidence acceptance/rejection, explicit promotion, invalid transitions, and
immutable result IDs. Formal master-spec checkboxes remain unchanged.
