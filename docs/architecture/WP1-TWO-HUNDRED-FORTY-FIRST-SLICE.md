# WP1 Two-Hundred-Forty-First Slice — thinking budget compatibility boundary

## Boundary

Moved the pure reasoning-budget exhaustion predicate from root `server.py` to
the packaged `sonder_runtime.domain.thinking_policy` boundary. The root
`server._thinking_exhausted_budget` name remains an identity-preserving alias
for legacy callers. Local request retry orchestration, payload budget sizing,
model capability tracking, and transport behavior remain in `server.py` and
are outside this slice.

## Evidence

- `tests/test_thinking_policy.py` verifies the packaged predicate's exact
  signature behavior and root alias identity.
- `pytest -q tests/test_thinking_policy.py tests/test_ensemble_answer.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
