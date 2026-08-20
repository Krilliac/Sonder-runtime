# WP1 seventh migration slice: backup, preflight, and workflow adapters

**Status:** Focused verification passed

## Scope

Retire the root compatibility aliases `sonder_backup.py`,
`sonder_preflight.py`, and `workflow_store.py`. Their package adapters are
already authoritative and callers/tests now import those package paths.

## Completed work

- [x] Add all three roots to the permanent retired-module architecture ratchet.
- [x] Rewire backup, preflight, workflow, and self-heal callers/tests.
- [x] Remove all three roots from the local-bundle inventory.
- [x] Remove compatibility-only identity assertions.
- [x] Move preflight pickle/type identities to the authoritative package port.
- [x] Extend the isolated architecture regression test to cover each root.

Focused verification: `78 passed, 5 skipped`; the consolidated WP1 regression
set is `186 passed, 5 skipped`; and `tests/production` is `296 passed, 4
skipped`. Architecture, evidence, and staged-diff checks pass.

## Remaining compatibility boundary

`memory_store.py` remains an intentional compatibility root because the
immutable `migrations/memory/0001_baseline.py` migration imports it. Removing
the root would break historical migration replay; changing that migration
would invalidate its recorded checksum. The architecture checker records this
single exception explicitly, while current production callers use the package
adapter.

The master-spec requirements remain unchecked; this slice is migration evidence,
not proof of a complete end-state requirement.
