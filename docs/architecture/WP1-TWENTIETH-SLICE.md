# WP1 Twentieth Slice: Package Operations Persistence

Status: implemented on `agent/wp1-execution-status`.

## Scope

The operations store now lives at
`sonder_runtime.adapters.persistence.operations_store`. Lifecycle, update,
backup, CLI, strangler-adapter, and production-test callers use the package
implementation. Root `sonder_operations_store.py` is retired; unlike the
autopilot and fleet stores, this boundary had no immutable migration import
requiring a compatibility alias.

## Evidence

- Operations, backup, lifecycle, update-engine, and production architecture
  regression: **94 passed**.
- `scripts/check_architecture.py`: passes with the root legacy ratchet reduced
  to 13.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The root legacy set still includes the fleet/autopilot compatibility aliases,
migration registry, lifecycle/update services, serving entrypoints, workbench,
and file-operation boundaries. Each remains an explicit next migration slice.
