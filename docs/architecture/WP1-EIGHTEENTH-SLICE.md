# WP1 Eighteenth Slice: Package Autopilot Persistence

Status: implemented on `agent/wp1-execution-status`.

## Scope

The canonical autopilot store now lives at
`sonder_runtime.adapters.persistence.autopilot_store`. Production callers,
the automation repository adapter, controller, server, and tests use that
package-qualified implementation. The root `autopilot_store.py` is only a
small delegation alias retained for the immutable
`migrations/autopilot/0001_baseline.py` migration; that exception is explicit
in the architecture checker and is tested alongside the memory migration
exception.

## Evidence

- Automation store, controller, server, steering, migration, domain, adapter,
  and architecture regression: **136 passed**.
- `scripts/check_architecture.py`: passes with the compatibility boundary
  explicitly allowlisted.
- `scripts/check_requirement_evidence.py`: passes.
- The staged diff has no whitespace errors.

## Remaining boundary

The root compatibility alias cannot be removed until the historical migration
format is replaced by an immutable package-native migration/archive strategy.
The remaining legacy stores and entrypoints continue under the shrink-only
architecture ratchet.
