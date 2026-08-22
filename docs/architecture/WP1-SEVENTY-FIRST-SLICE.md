# WP1 Seventy-First Slice: Preference Database Path Boundary

Status: implemented on `agent/wp1-execution-status`.

## Scope

The packaged preference adapter now resolves its default memory database
through `sonder_runtime.platform.paths`. The path boundary still delegates to
the compatibility-backed implementation, preserving the existing database
selection and connection settings while removing this packaged adapter's
direct root `sonder_paths` import.

## Evidence

- Preference adapter and service regressions: focused tests pass.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check` and `git diff --check`: pass.

## Remaining boundary

The platform path module remains the compatibility-backed implementation, and
other adapters retain separate path migrations where their contracts require
them. This slice changes only the preference adapter's default database-path
caller.
