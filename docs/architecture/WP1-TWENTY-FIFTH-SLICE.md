# WP1 Twenty-Fifth Slice: Package Update Orchestration

Status: implemented on `agent/wp1-execution-status`.

## Scope

The update orchestration engine now lives at
`sonder_runtime.adapters.updates.engine`. Update serving, the package CLI,
backup-service, path-portability, TUF, manifest-trust, and update-engine tests
use the package-qualified module. Root `sonder_update_engine.py` is retired.

The engine remains an adapter because it owns subprocess-based health checks.
The architecture policy gives only this exact file permission to use
`subprocess` and to call the bootstrap health-check composition hook.

## Evidence

- Update, backup-service, path-portability, TUF, manifest-trust, schema-guard,
  and production architecture regression: **143 passed, 11 skipped**.
- `scripts/check_architecture.py`: passes with the root legacy ratchet reduced
  to 8.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The remaining roots are server, immutable autopilot/fleet aliases, migration
registry, lifecycle, serving, and REPL. The update stack is now entirely under
`sonder_runtime`.
