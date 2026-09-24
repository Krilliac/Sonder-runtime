# DATA-005 migration evidence

Scope: the real SQLite bridge migration and application-owned backup/restore
proofs in this isolated checkout. No production migration code was changed.

Command (from repository root, with an isolated environment):

```text
python -m pytest -q tests/test_data005_migration_rehearsal.py tests/test_remaining_migration_safety.py tests/test_epoch2_migration_entrypoint.py
9 passed in 2.87s
```

Evidence established:

- A disposable canonical-domain home containing real SQLite files completes
  the bridge rehearsal. The existing public `rehearse_bridge_migration`
  service verifies the pre-migration backup, restores it into an independent
  directory, restores a second crash-recovery copy, resumes the interrupted
  migration in place, and verifies epoch-2 ownership/cleanup across the
  adopted databases.
- A tampered backup member is rejected by
  `verify_backup_before_migration` before any source write; the source bytes
  remain unchanged.
- A real child Python process exits with code 77 at the
  `after_data_adoption` boundary of the production migration entrypoint. The
  parent reopens the files, finds the
  pre-epoch backup, checks every member digest, and proves an independent
  restore with `prove_restore`.
- Existing epoch-adoption entrypoint coverage remains green and verifies the
  public command writes its adoption receipt only on explicit epoch-2 use.

Limits:

- The crash process is exercised through a real subprocess; POSIX-specific
  filesystem behavior is not claimed.
- The rehearsal's injected boundary is a supported migration test seam; the
  subprocess crash test supplies the hard process-exit evidence.
- Backup integrity is verified by the application proof and bridge copy
  digest checks. A future production rollout still needs operator-specific
  backup media and source ownership qualification.
