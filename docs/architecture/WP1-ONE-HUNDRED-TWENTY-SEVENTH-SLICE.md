# WP1 one-hundred-twenty-seventh slice — UnitOfWork adapter ownership

The stateful `LegacyUnitOfWork` implementation now lives in the canonical
`sonder_runtime.adapters.unit_of_work.UnitOfWorkAdapter`.  Bootstrap composes
the canonical adapter directly.  `strangler_services.LegacyUnitOfWork` remains
only as an identity-preserving compatibility alias for existing callers.

The adapter preserves the existing connection lifecycle, repository wiring,
commit/rollback behavior, and packaged memory-path boundary.  This slice does
not modify `server.py` or any previously migrated repository, tool, event, or
model-gateway implementation.

## Verification

- Focused UnitOfWork, memory, path, and composition tests passed.
- Compile, architecture, requirement-evidence, and diff gates passed.
- This note records migration evidence; it does not credit a master-spec
  checkbox.

