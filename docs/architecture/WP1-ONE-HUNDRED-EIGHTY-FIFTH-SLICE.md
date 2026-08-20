# WP1 One-Hundred-Eighty-Fifth Slice

## Boundary

Moved the pure valid-tier-name presentation policy from root `server.py` into
the packaged domain module `sonder_runtime.domain.tier_names`. The server
retains its compatibility helper and continues to own live tier discovery and
cloud-availability filtering.

## Evidence

- `tests/test_tier_names.py` verifies mapping and iterable formatting plus the
  root compatibility delegate.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.

## Scope

This slice changes only the tier-name presentation boundary. It does not move
tier discovery, cloud policy, model routing, or any boundary covered by prior
WP1 slices.
