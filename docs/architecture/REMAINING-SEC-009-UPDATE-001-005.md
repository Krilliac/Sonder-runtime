# Remaining SEC-009 / UPDATE-001–005 evidence

## Scope

This slice adds two application-boundary contracts without changing the
formal master checklist.

### SEC-009 — Recovery boundary

`application/security/recovery_boundary.py` emits a typed assessment for a
recovery attempt. Same-user recovery is explicitly classified as an
operational continuity aid, never as a security boundary. Audit paths are
evidence only. When explicitly unrestricted self-modification is enabled,
the assessment states that the actor can alter recovery state and audit files;
an external enforcement mechanism is required for security.

### UPDATE-001 / 003 / 005 — Release evidence

`application/updates/release_evidence.py` binds a release to:

- a canonical signed manifest and artifact SHA-256 hashes;
- a deterministic SBOM inventory;
- bounded test results;
- migration requirements; and
- tested rollback compatibility and restore proof.

`ReleaseEvidencePackage.verify` rechecks the package digest, signature,
failed-test state, rollback test state, and (when supplied) the exact sealed
runtime dependency contract.

### UPDATE-002 / 004 — Platform activation and atomic rollback

`PlatformActivationHelper` is an injected out-of-process contract for Linux,
Windows, and macOS. `AtomicReleaseActivator` verifies the current known-good
route, delegates activation to the helper, commits the pointer only after
helper success, and invokes the independent helper to restore the prior route
if activation or pointer commit fails. It never relies on the failed runtime
for recovery and performs no platform command or filesystem I/O itself.

## Verification

Focused coverage is in `tests/test_remaining_recovery_updates.py`:

- same-user and unrestricted recovery never receive a security-boundary claim;
- release signature, SBOM/test/migration/rollback evidence is validated;
- all three supported platform labels use the same helper contract; and
- failed activation leaves the previous release selected after helper rollback.

Formal checklist edits are intentionally out of scope.
