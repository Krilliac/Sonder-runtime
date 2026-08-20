# WP1 Ninety-Eighth Slice: Structured Logging Ownership

The implementation of structured logging, secret redaction, and child-
environment filtering now lives in `sonder_runtime.platform.logging`.
The root `sonder_logging` module is a thin module-identity compatibility shim,
so legacy imports and private compatibility monkeypatches still target the
canonical implementation. Logger identity, handler replacement, JSON
formatting, redaction failure behavior, and import-time behavior are preserved.

## Evidence

- `tests/test_logging_platform_seam.py` verifies canonical module identity,
  legacy private-pattern patching, handler setup, and redaction.
- `tests/production/test_logging_redaction.py` covers the existing structured
  logging and fail-closed redaction contract.
- `python -m compileall -q sonder_runtime server.py`.
- `python scripts/check_architecture.py`.
- `python scripts/check_requirement_evidence.py`.
- `git diff --cached --check` and `git diff --check`.

The root logging compatibility shim is no longer an implementation owner and
is removed from the package's root-platform allowance.
