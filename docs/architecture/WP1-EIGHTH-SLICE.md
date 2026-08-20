# WP1 eighth migration slice: storage diagnostics adapter

**Status:** Focused verification passed

## Scope

Retire the root `sonder_storage.py` delegate. Storage diagnostics already live in
`sonder_runtime.adapters.storage`; the two lazy doctor callers now import that
authoritative package path directly.

## Completed work

- [x] Rewire both `sonder_doctor.py` storage checks.
- [x] Add the root to the permanent retired-module architecture ratchet.
- [x] Add an isolated regression assertion for the retired root.
- [x] Delete the root delegate without adding a compatibility shim.

The master-spec requirements remain unchecked; this slice is migration evidence,
not proof of a complete end-state requirement.

Focused verification: `62 passed, 4 skipped`; architecture, evidence, and
staged-diff checks pass.
