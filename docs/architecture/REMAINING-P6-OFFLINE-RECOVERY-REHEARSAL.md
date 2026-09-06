# P6 offline recovery rehearsal

This slice adds a provider-neutral application contract for rehearsing a
backup restore followed by an upgrade failure and rollback.  The contract is
implemented by `application/updates/recovery_rehearsal.py`; the
`FilesystemOfflineRecoveryPort` adapter supplies the local filesystem and
SQLite backup behavior.

The rehearsal is deliberately disposable.  It reads an existing backup,
checks the manifest and checksum index through the existing verified backup
adapter, restores into a caller-selected fenced workspace, simulates a
candidate release with a marker, restores the authoritative state after the
simulated failure, and removes only the bounded rehearsal tree.  It never
switches the live release pointer, starts a service, contacts a model or
other provider, or participates in single-PC/two-PC failover.

The application contract requires these ordered steps:

1. inspect the backup manifest;
2. verify the manifest identity and source revision;
3. verify every manifest artifact and the checksum index;
4. restore to a disposable staging directory;
5. independently verify every restored artifact and its source revision;
6. apply the candidate upgrade;
7. on the expected failure, roll back the candidate release marker;
8. restore and verify the authoritative state from the same backup; and
9. clean the disposable tree within the caller's entry bound.

A revision mismatch or corrupt artifact is rejected before a staging write.
The source revision is recorded in new backups as `source_revision`, with a
version fallback for source checkouts without Git metadata; older manifests
remain readable through their commit/version identity.  The report includes
the manifest digest, checksum digest, restore digest, ordered steps, and
bounded cleanup receipt.

The application limits one rehearsal to 64 artifacts and 1 GiB of declared
state.  Cleanup is capped at 256 entries (or a lower request bound), and an
adapter must leave the tree in place rather than delete beyond that bound.
These are rehearsal limits, not a claim about disaster-recovery throughput or
live high-availability behavior.

Focused coverage is in `tests/test_offline_recovery_rehearsal.py`.  Existing
`tests/production/test_backup.py` and update-engine suites remain the
authoritative checks for live backup creation, update installation, and
operator rollback.
