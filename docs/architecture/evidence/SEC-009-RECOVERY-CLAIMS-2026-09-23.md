# SEC-009: recovery and audit claims under unrestricted self-modification

SEC-009 requires that Sonder never claim same-user recovery or audit files are
a security boundary against explicitly unrestricted selfmod. This is a
truthfulness requirement: the protection is the absence of a false claim, in
typed results, in persisted evidence, and in the documents operators and
agents read. The earlier repository evidence is in
[SEC-009-RECOVERY-EVIDENCE-REPOSITORY-2026-08-21.md](SEC-009-RECOVERY-EVIDENCE-REPOSITORY-2026-08-21.md).
The requirement-wide audit is in [SEC-AUDIT-2026-09-23.md](SEC-AUDIT-2026-09-23.md).

## Defects found and fixed

1. **Verification dropped the unrestricted disclosure.**
   `FilesystemRecoveryEvidenceRepository.verify` built its boundary assessment
   without the unrestricted flag. A record verified while
   `--unrestricted-selfmod` was active therefore reported only the same-user
   limitation, and presented "verified" without saying that the verifying
   process could have rewritten the artifact and the audit chain together.
   The repository now takes the frozen startup capability. Every record it
   returns, from `record` or `verify`, carries the unrestricted limitation, and
   a per-call `False` cannot remove it. Review round 1 made the capability a
   required keyword with no default, so a composition root that forgets it
   fails instead of silently dropping the disclosure.
2. **Operator and agent documents overclaimed.** `SELFMOD.md` described
   "immutable backups" and did not mention `--unrestricted-selfmod`. The selfmod
   skill said "immutable backup" and "append-only audit" without qualification,
   and `selfmod_service` persisted the note "legacy immutable backup verified".
   These now say "hash-verified". `SELFMOD.md` has a section, "Recovery and
   audit are not a security boundary", and `SECURITY.md` links to it.

## Evidence

`tests/test_sec009_recovery_boundary_claims.py` (23 cases):

- the typed contract refuses a declared boundary for every actor shape,
  `dataclasses.replace` cannot add one, a forged field is ignored by
  `can_claim_security_boundary` and rejected by `RecoveryEvidenceRecord`, and
  non-bool flags are rejected rather than coerced;
- the repository keeps the unrestricted disclosure through `verify`, cannot
  have it downgraded per call, and cannot be constructed without the startup
  capability;
- the honest limit is stated as a test: a consistent same-user rewrite of the
  artifact and the whole audit chain still verifies, and the record carries
  the limitation instead of implying that the rewrite was detected;
- a claim ratchet scans every tracked Markdown file and the string and comment
  tokens of `sonder_runtime/`, `selfmod*.py`, and `scripts/*selfmod*.py` for
  affirmative assurance language about recovery, backup, or audit material,
  with tests that it detects overclaims, accepts limited statements, and
  covers `SELFMOD.md`, `SECURITY.md`, `selfmod.py`, and `selfmod_recover.py`;
- `SELFMOD.md` and `SECURITY.md` must both state the limit against
  `--unrestricted-selfmod`.

RED/GREEN: run against the unchanged implementation and documents, 5 cases
failed: the verify disclosure, the per-call downgrade, the rewrite
disclosure, the repository ratchet, and the operator-document disclosure.
All 22 pass after the change. Review round 1 added the required-keyword case,
which failed before the default was removed and passes after. `tests/test_sec009_recovery_evidence_repository.py`,
`tests/test_remaining_sec_009.py`, `tests/test_remaining_recovery_updates.py`,
and `tests/test_selfmod_deploy_health.py` also pass (41 with the new file).
The selfmod suites `tests/production/test_release_hardening.py`,
`tests/test_control_plane_authorities.py`,
`tests/test_selfmod_legacy_integration.py`, `tests/test_spec5_selfmod.py`,
`tests/test_worker_effect_bindings.py`, and `tests/test_data_inspect.py`
report 71 passed and 1 skipped (needs a container runtime).

## Remaining gap (why SEC-009 stays unverified)

The five pinned overclaims are resolved. PR #519 reworded the five backup
descriptions in `selfmod.py` and `scripts/nightly_selfmod.py` as
"tamper-evident" or "best-effort". After this
branch merged `main`, the ratchet failed exactly as designed: it found zero
where it expected five. `KNOWN_DEBT` is now empty, so any new finding fails
the test.

What remains is production wiring. No production caller builds
`FilesystemRecoveryEvidenceRepository` with the live startup
`--unrestricted-selfmod` capability, and the self-modification recovery path
(`selfmod.py`, `selfmod_recover.py`) does not emit `RecoveryBoundary`
assessments. The typed disclosure is therefore proven as a contract, not as
runtime behavior. SEC-009 stays `implemented_unverified` until that path
carries the assessment, and a green CI run on the exact SHA proves it.

## Limitations

`actor` and `resource_owner` are caller-supplied application labels, not
operating-system identities derived from the process. This is documented on
`RecoveryBoundary` and the repository adapter. The labels only choose which
limitations are disclosed. They never authorize anything, and since a
same-user process can pass any label, no assessment may claim a boundary.

The ratchet is lexical. It catches the assurance phrasings it lists, not
every way a sentence could imply protection, and a negation anywhere in a
sentence satisfies it. Python coverage is limited to string and comment
tokens in the named paths. The repository has no production caller that
constructs the evidence repository with the live startup capability yet; the
capability is threaded through `bootstrap` only for the selfmod service.
