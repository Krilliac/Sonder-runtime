# WP1 learning-health ownership move — 2026-08-21

## Scope

The canonical implementation of the read-only learning-health report now
lives at `sonder_runtime/adapters/learning_health.py`. The root
`learning_health.py` file is an identity-preserving compatibility alias for
legacy callers, including the server route and existing test monkeypatch
seams.

The move preserves the existing provenance boundaries: caller-judged,
machine-attributed, self-curriculum, and unknown outcome sources remain
separate. The report continues to fail closed when evidence is insufficient or
embedding provenance is invalid, and its human-readable formatter remains
owned by the same implementation.

## Evidence

- `tests/test_learning_health.py` — existing report, provenance, fail-closed,
  and formatting coverage.
- `tests/test_outcome_source.py` — caller-source and unknown-population
  regressions.
- `tests/test_learning_health_ownership.py` — canonical module identity,
  private provenance seam, and reload compatibility.
- `scripts/check_architecture.py` — narrow compatibility and dependency
  ratchets for the adapter.

## Verification boundary

This is `implemented_unverified`: focused and architecture checks prove the
ownership move and compatibility contract, but the complete production
requirement remains subject to the broader end-to-end receipt and deployment
audit.
