# SELFMOD-001/003/004 bounded legacy integration — 2026-08-21

## Scope

This slice connects the typed reproducer, governance, and verification
lifecycle contracts to the existing guarded `selfmod.py` execution surface.
The application service owns only typed orchestration. Bootstrap supplies a
lazy port to the root module, which remains authoritative for backups,
isolated workspaces, command execution, review, deployment, and rollback.

## Guarded behavior

- A `FailureEvidence` reproducer is validated before the legacy baseline
  reproducer is accepted.
- The guarded bridge requires targeted, architecture, regression, and smoke
  verification records before invoking legacy review.
- Independent review and typed backup evidence are required before approval.
- Guarded deployment requires an explicit post-deployment health command.
- Legacy automatic rollback is translated into typed health-failure and
  rollback evidence when deployment health fails.
- The bridge performs no file writes, subprocess calls, Git operations, or
  remote pushes itself.

## Unrestricted behavior

The `unrestricted=True` bridge delegates review, approval, deployment, and
rollback to the existing legacy path without imposing the new guarded typed
gates. This preserves the existing explicit bypass semantics; it does not
grant guarded callers any additional authority.

## Evidence

- `tests/test_selfmod_legacy_integration.py`: 4 focused integration tests.
- `tests/test_selfmod_governance_reproducer.py`,
  `tests/test_selfmod_verification_lifecycle.py`, and
  `tests/test_remaining_selfmod_governance.py`: 36 tests passed together.
- `python scripts/check_architecture.py`: passed after the bootstrap import
  was kept lazy through `importlib`.
- `python -m compileall -q sonder_runtime/application/selfmod
  sonder_runtime/bootstrap`: passed.

## Limitations

The typed lifecycle is process-local and its receipts are not yet persisted
as a separate durable ledger. Existing root selfmod SQLite events and backup
manifests remain the durable operational record. Full end-to-end integration
through every legacy server command surface remains outside this bounded
bootstrap slice, so SELFMOD-001/003/004 remain `implemented_unverified`.
