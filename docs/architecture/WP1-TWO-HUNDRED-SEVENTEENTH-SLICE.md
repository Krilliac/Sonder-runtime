# WP1 Two-Hundred-Seventeenth Slice — Query Limit Policy Ownership

## Boundary

Moved the pure bounded integer-limit normalizer from `server.py` into
`sonder_runtime.domain.query_limits`. The root module retains `_safe_limit` as
a compatibility delegate for existing tool callers.

## Invariants

- Invalid input still falls back to the caller-provided default.
- Results remain clamped to the inclusive range from one through the supplied
  maximum.
- String integer input and custom defaults/ceilings retain their prior behavior.
- No tool dispatch, database, or filesystem behavior changed.

## Evidence

- `python -m pytest tests/test_query_limits.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
