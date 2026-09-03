# WP1 Three-Hundred-Thirty-Sixth Slice — approvals limit

## Boundary

Moved `_approvals_limit` (parse and clamp approval listing limit) into `sonder_runtime/domain/approvals_limit.py` as `approvals_limit`. Pure function with zero dependencies. The root `_approvals_limit` name is now an identity-preserving alias.

## Evidence

- `tests/test_approvals_limit_boundary.py` verifies alias identity, defaults, clamping, and non-numeric input.
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime tests server.py`
- `git diff --check`
