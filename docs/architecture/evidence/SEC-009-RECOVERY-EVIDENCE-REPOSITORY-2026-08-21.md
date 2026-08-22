# SEC-009 — Typed durable recovery evidence repository

## Bounded implementation

`FilesystemRecoveryEvidenceRepository` composes the existing
`RecoveryArtifactService` into a typed repository contract.  Artifact paths,
owner checks, payload bounds, and the chained audit file remain owned by the
existing artifact service; the repository returns a durable
`RecoveryEvidenceRecord` containing the verified artifact, absolute path, and
the boundary assessment.

## Truth boundary

The record is explicitly `tamper_evident_only`.  The application owner and
actor labels do not establish an operating-system authorization boundary.  A
same-user or explicitly unrestricted self-mod actor may replace recovery
artifacts and audit files together, so the repository never claims immutable
audit, independent authority, or same-user security.  Verification fails
closed when the existing artifact digest or chained audit integrity fails.

## Evidence

`tests/test_sec009_recovery_evidence_repository.py` proves typed owner/path
composition, audit-chain continuity, fail-closed payload tampering, foreign
actor rejection, and explicit same-user disclosure for unrestricted recovery.
