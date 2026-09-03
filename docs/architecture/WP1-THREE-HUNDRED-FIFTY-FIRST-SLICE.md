# WP1 Three-Hundred-Fifty-First Slice — schema coverage annotation

## Boundary

`_with_schema_coverage` moved from `server.py` into the existing
`sonder_runtime/domain/schema_policy.py` as `with_schema_coverage`, alongside
the already-packaged `format_schema_gaps` and `leading_json_object`.

The root name `server._with_schema_coverage` is now an identity-preserving
alias (`from ... import with_schema_coverage as _with_schema_coverage`).

Pure text formatting: appends schema-unverified gap annotations to output text,
or returns text unchanged when there are no gaps. No environment reads, no I/O.

## Evidence

- `tests/test_schema_coverage_boundary.py` verifies identity-preserving alias
  (`server._with_schema_coverage is with_schema_coverage`), no-gaps passthrough,
  gap appending, empty-gaps passthrough, and multiple-gap formatting.
- `python -m pytest -q tests/test_schema_coverage_boundary.py` — 5 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime tests` — silent, exit 0
- `git diff --check` — silent, exit 0
