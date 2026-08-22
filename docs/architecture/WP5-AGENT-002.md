# WP5-AGENT-002 — Workbench and review registry adapters

This slice adds a provider-neutral adapter for the two existing modes. Both
are registered through one `WorkbenchReviewAdapter` contract, while retaining
their distinct side-effect envelopes:

- `workbench` is an editor with a bounded workspace mutation policy.
- `review` is a bounded, read-only reviewer.

The adapter validates mode names, correlation IDs, prompt/context sizes, and
metadata shape before producing an `AgentInvocation`. Authorization, model
routing, persistence, and execution remain owned by the shared registry and
its downstream ports. No legacy loop or formal specification checkbox is
changed by this slice.

Evidence: `tests/test_wp5_workbench_review.py` covers registration, mode
normalization, read-only review semantics, metadata determinism, and bounds.
