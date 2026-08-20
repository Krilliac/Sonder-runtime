# WP1 Eighty-Eighth Slice: operations-store logging seam

`sonder_runtime.adapters.persistence.operations_store` now consumes
`Redactor` and `REDACTION_FAILED` through the canonical
`sonder_runtime.platform.logging` boundary. That boundary re-exports the
same root implementation objects, so redaction behavior and durable event
persistence semantics remain unchanged.

## Evidence

- `tests/test_operations_store_logging_seam.py`
- operations-store event, backup-run, and maintenance-lock regressions
- `python -m compileall -q sonder_runtime server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `git diff --cached --check`
- `git diff --check`
