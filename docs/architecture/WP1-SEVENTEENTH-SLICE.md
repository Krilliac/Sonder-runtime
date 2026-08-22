# WP1 Seventeenth Slice: Package Response Activity Tracking

Status: implemented on `agent/wp1-execution-status`.

## Scope

The response activity tracker now lives at
`sonder_runtime.adapters.observability.activity_tracker`. Root-level
`activity_tracker.py` is retired. Server, REPL, serving, NPU, and activity
test callers use the package-qualified module, while the server live-reload
list refreshes the same packaged authority.

## Evidence

- Activity, verification-gate, server-helper, NPU, and production architecture
  regression: **449 passed**.
- `scripts/check_architecture.py`: passes with the root activity module
  permanently retired.
- The NPU service and activity tracker now share the package boundary without
  a root compatibility import.

## Remaining boundary

The immutable baseline migration's `memory_store.py` compatibility exception
remains documented separately. Other root entrypoints and legacy stores are
still governed by the explicit architecture ratchet and require their own
behavior-preserving slices.
