# WP6-EVAL-001 — deterministic trajectory replay

The application evaluation boundary now provides immutable `TrajectoryStep`
and `TrajectoryRecord` values. JSON-canonical serialization and SHA-256
digests make equivalent inputs, outputs, metadata, and state compare
deterministically across processes. `replay_trajectory` re-runs recorded
inputs through a caller-owned evaluator and returns a non-throwing
`ReplayReport` with step-indexed output divergences. `compare_trajectories`
also detects metadata, identity, input, state, and length differences.

This slice intentionally does not write evaluation history, promote models, or
silently reconstruct missing outputs. Existing evaluation-history status and
session replay contracts remain unchanged.

Validation: `tests/test_wp6_trajectory_replay.py` plus architecture/evidence,
compileall, and diff checks.
