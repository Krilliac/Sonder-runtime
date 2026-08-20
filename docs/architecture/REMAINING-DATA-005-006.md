# DATA-005/006: crash-safe migration and epoch adoption

`sonder_runtime/application/persistence/migration_safety.py` defines the
application boundary for the migration safety slice. It does not own SQLite,
backup creation, restore mutation, or bridge deletion.

Before destructive adoption, `verify_backup_before_migration` requires an
adapter verifier to report no problems and captures SHA-256 digests for every
live source file. A `BackupProof` is therefore evidence, not an instruction to
mutate state. `prove_restore` independently hashes every restored member,
rejects missing or unexpected coverage, and refuses any digest mismatch.

`adopt_schema_epoch` binds a canonical receipt digest to a complete backup and
accepts only the supported epoch 2. A source epoch newer than the supported
epoch raises `FutureSchemaError`; the runtime never guesses how to interpret
future state. `decide_bridge_cleanup` returns an explicit denial until epoch
adoption, backup verification, independent restore proof, receipt presence,
and bridge acceptance tests all exist. Cleanup remains an external release
operation and is never performed implicitly by this module.

Focused coverage is in `tests/test_remaining_migration_safety.py`.
No formal specification checkbox is changed by this slice.
