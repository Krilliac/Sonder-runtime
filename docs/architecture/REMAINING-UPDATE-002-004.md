# UPDATE-002 / UPDATE-003 / UPDATE-004 evidence

## Scope

This slice closes the signed-update gaps without changing the master checklist
or the requirement audit.

### UPDATE-002 — platform-neutral helper-process activation

`ActivationRequest` carries the platform label, known-good and target release,
release-evidence digest, nonce, and optional helper argv. `PlatformActivationHelper`
is the injected out-of-process boundary. The application contract contains no
subprocess, shell, symlink, or platform-specific filesystem operation, so the
same request/rollback protocol is usable by Linux, Windows, and macOS helpers.

### UPDATE-003 — exact sealed runtime dependencies

`SealedRuntimeContract` canonicalizes a non-empty dependency map and seals its
sorted entries with a digest. Verification rejects missing, extra, changed, or
tampered entries. `ReleaseEvidencePackage.verify` uses this exact comparison
when an expected runtime contract is supplied, before evidence is accepted for
stamping or activation.

### UPDATE-004 — atomic activation and standalone recovery evidence

`AtomicReleaseActivator` checks the current pointer before invoking the helper,
commits the target only after helper success, and invokes the independent helper
plus pointer store to restore the previous release after any activation or
commit failure. Every recovery attempt emits immutable
`StandaloneRecoveryEvidence`; an incomplete pointer restore raises
`ActivationRecoveryError` instead of being hidden behind the original failure.

## Verification

Focused coverage is in `tests/test_remaining_update_002_004.py` and exercises:

- exact dependency equality, missing/extra entries, and sealed-digest tampering;
- all platform-neutral activation request fields without executing a helper;
- successful independent rollback evidence; and
- explicit incomplete recovery when both helper rollback and pointer restore
  fail.

No network, subprocess, real release pointer, or system mutation is used.
