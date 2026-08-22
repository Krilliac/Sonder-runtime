# WP1 Sixty-Eighth Slice: Evaluation-history package manifest boundary

## Status

Implemented locally; focused package verification passed.

## Scope

The retired root `eval_history.py` is no longer a package payload. The local
system manifest now has regression coverage proving that the canonical
evaluation-history store, application service, and reader port are shipped
instead.

This preserves the retired-root policy established by the fifth slice while
protecting the packaged runtime from silently losing the replacement package.

## Verification

- `python -m pytest -q tests/test_package_local_system.py` — passed.
- `python -m compileall -q sonder_runtime server.py` — passed.
- `python scripts/check_architecture.py` — passed.
- `python scripts/check_requirement_evidence.py` — passed.
- `git diff --cached --check` and `git diff --check` — passed.
