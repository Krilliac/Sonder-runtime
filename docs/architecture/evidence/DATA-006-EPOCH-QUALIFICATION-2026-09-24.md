# DATA-006 schema-epoch qualification

This note records a focused qualification delta at baseline `ce0291bf`.

The master acceptance for DATA-006 requires schema epoch 2 adoption, removal
of temporary bridge code, and explicit refusal of unsupported future schemas.
The production entrypoint exercised here is
`sonder_runtime.adapters.persistence.sqlite.bridge_migration.require_epoch_2`,
against its actual adopted-domain catalog (`memory.db`, `automation.db`,
`operations.db`, `selfmod.db`, and `training.db`).

Command, serial with isolated pytest basetemp:

```text
python -m pytest -q tests/test_data006_epoch_qualification.py --basetemp=<isolated-temp>
```

Result: **7 passed in 1.97s** in the bounded qualification run.

The supported-state test runs the production bridge migration on a disposable
real-file home, then reopens the epoch-2 catalog through `require_epoch_2` and
asserts that the database files are byte-identical. The parameterized refusal
tests set one catalog database to future epoch 3, call the same production
entrypoint, assert `MigrationRequired`, and compare all main database bytes
before and after. SQLite `-wal`/`-shm` transport sidecars are excluded from
the byte snapshot because opening a WAL database may update those runtime
sidecars without changing database schema or data.

This qualifies supported epoch-2 admission and future-epoch refusal for the
five databases in the production `EPOCH2_DATABASES` constant. It does not
claim that `updates.db` belongs in that destructive bridge catalog. The update
owner uses the separate checksummed `migrations/updates/0001_baseline.py`
ledger through the production `UpdateRepository`; the added regression proves
supported update-plan reopen and refuses an unknown future migration before
writing. Complete migration cleanup/bridge removal is not demonstrated, and
restore rehearsal or cross-platform deployment qualification remains open.
These focused tests do not promote the broader master requirement to verified.
