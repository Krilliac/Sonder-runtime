# WP1 Twenty-Third Slice: Package Secret Rotation

Status: implemented on `agent/wp1-execution-status`.

## Scope

Secret rotation now lives at `sonder_runtime.adapters.secrets`. Admin
authentication, serving, the package CLI, and secret-rotation tests use the
packaged adapter. Root `sonder_secrets.py` is retired.

## Evidence

- Secret rotation, admin-authentication, and production architecture
  regression: **63 passed, 2 skipped**.
- `scripts/check_architecture.py`: passes with the root legacy ratchet reduced
  to 10.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

Lifecycle, serving, REPL, update, migration, and server entrypoints remain
explicit root boundaries for later slices.
