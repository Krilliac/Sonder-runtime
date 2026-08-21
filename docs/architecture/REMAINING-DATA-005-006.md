# DATA-005/006: crash-safe migration and epoch adoption

The implementation is deliberately split across two persistence paths:

- `adapters/persistence/migration_rehearsal.py` is the DATA-005 rehearsal
  harness. It copies an operator-selected home to disposable directories,
  invokes the existing `sqlite.bridge_migration.run_bridge_migration`, stops
  at an explicit fault boundary, and independently verifies/restores the
  bridge backup. The supplied home is never opened for writing.
- `adapters/persistence/epoch_adoption.py` is the DATA-006 read-only checker.
  It verifies epoch 2 on every adopted database, validates the adoption
  receipt, checks known migration ledgers through `status_read_only`, enforces
  the runtime epoch gate, and reports temporary schema objects or paths that
  still exist. It never deletes them.
- The designated operator entrypoint is explicit:
  `python -m sonder_runtime migrate --adopt-epoch2`. It binds the typed state
  home, runs the bridge without fault injection, and refuses success unless the
  post-adoption checker proves every epoch marker, receipt, ledger, and cleanup
  invariant. Ordinary `migrate` and `serve` do not silently perform adoption.
  `serve` now applies the epoch gate before migration or listener binding and
  directs pre-epoch homes to this explicit command.

The bridge migration has an explicit optional `step_hook` used only by the
rehearsal. Normal runtime calls leave it unset, so the production migration
path has no fault injection or cleanup side effect.

Before destructive adoption, `verify_backup_before_migration` requires an
adapter verifier to report no problems and captures SHA-256 digests for every
live source file. A `BackupProof` is therefore evidence, not an instruction to
mutate state. `prove_restore` independently hashes every restored member,
rejects missing or unexpected coverage, and refuses any digest mismatch.

The existing `migration_safety` contracts remain the storage-neutral proof
types: `adopt_schema_epoch` accepts only epoch 2 and rejects future schemas;
`decide_bridge_cleanup` denies cleanup until adoption, backup, independent
restore, receipt, and bridge tests are all proven. The new adapter checker
turns those requirements into an executable post-rehearsal report without
performing cleanup.

Focused coverage is in `tests/test_remaining_migration_safety.py`,
`tests/test_remaining_data_005_006.py`, and
`tests/test_epoch2_migration_entrypoint.py`. The latter proves explicit CLI
adoption, fresh-install epoch stamping, backup/restore, fault-boundary
recovery, source immutability, epoch-2 receipt checks, and temporary-schema
rejection.
No formal specification checkbox is changed by this slice.
