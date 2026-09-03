# WP1 Three-Hundred-Thirty-Seventh Slice — callable inspection

## Boundary

Moved `_callable_accepts_keyword` (keyword argument introspection for test double compatibility) into `sonder_runtime/domain/callable_inspection.py` as `callable_accepts_keyword`. Uses only stdlib `inspect`. The root `_callable_accepts_keyword` name is now an identity-preserving alias.

## Evidence

- `tests/test_callable_inspection_boundary.py` verifies alias identity, declared/undeclared keywords, VAR_KEYWORD, and unsignable callables.
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime tests server.py`
- `git diff --check`
