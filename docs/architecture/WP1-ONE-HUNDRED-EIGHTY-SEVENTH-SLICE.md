# WP1 One-Hundred-Eighty-Seventh Slice

## Boundary

Moved the pure learning-enablement policy from root `server.py` into the
packaged domain module `sonder_runtime.domain.learning_tier`. The server keeps
the live `LEARN_TIERS` environment projection and retains its compatibility
wrapper.

## Evidence

- `tests/test_learning_tier_policy.py` verifies opt-in behavior, configured-tier
  membership, live server configuration, and the policy import boundary.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.

## Scope

This slice changes only the pure learning-enablement policy. It does not move
environment configuration, tier discovery, model routing, learning storage,
formatting, or any boundary covered by prior WP1 slices.
