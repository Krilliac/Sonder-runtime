# Selfmod recovery preflight evidence — 2026-08-21

## Change

`selfmod_recover.restore` now performs a bounded, read-only preflight over the
entire manifest before changing the repository. It rejects malformed records,
duplicate or non-relative repository paths, paths escaping the repository or
recovery bundle, missing or oversized backups, and backup/hash/size mismatches.

The existing same-directory temporary-file plus `os.replace` operation remains
the per-file write primitive. The new preflight closes the partial-restore
failure mode where an earlier record could be restored before a later corrupt
record was discovered.

## Evidence

- Focused regression: `tests/test_selfmod.py::test_emergency_recovery_preflights_every_backup_before_mutating`
- Existing emergency path regression: `tests/test_selfmod.py::test_emergency_recovery_does_not_import_application`
- Expected behavior: corrupting the second backup raises before either target is
  changed; recovery remains fail-closed.

## Scope and limitation

This is an integrity and blast-radius improvement, not an independent
authorization boundary. The recovery bundle and repository are still controlled
by the same Windows user/account. The current environment also has an ACL
limitation that prevents reliable Git worktree creation under the test
workspace; this change does not create or require a worktree and therefore does
not claim to resolve that limitation.
