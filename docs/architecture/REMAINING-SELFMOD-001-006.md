# Remaining self-modification governance (SELFMOD-001–006)

## Scope

This slice closes the remaining application-level governance gap around
self-modification candidates. It is intentionally isolated from the existing
`SelfModificationService`: the existing service owns the legacy lifecycle,
while `SelfmodGovernance` supplies an evidence-first authorization boundary
for new callers.

## Contract

`SelfmodGovernance` records the following lifecycle in order:

1. propose a candidate with a baseline digest;
2. attach externally supplied worktree metadata;
3. record an independently produced verification result;
4. record a review that cites known verification evidence;
5. approve only after the preceding gates; and
6. emit a local deployment intent.

The module is persistence-neutral and has no filesystem, subprocess, git,
network, deployment, or remote-push side effects. Worktree metadata describes
an adapter result; governance does not create or remove the worktree.

## Gate semantics

- Guarded candidates require an isolated, clean, adapter-managed worktree, a passing
  verification, and an approving review citing that verification.
- A failed guarded gate rejects the candidate and cannot be converted into an
  approval through a later call.
- A review cannot cite unknown evidence IDs.
- `unrestricted=True` is an explicit boundary, not a synthetic pass. It
  permits the caller to proceed with non-isolation, failed verification, or
  rejected review, while recording each bypass on both the candidate and the
  deployment intent. It still requires the lifecycle records to exist, so
  missing evidence is not silently represented as passed evidence.
- Deployment intent always has `automatic_push=False` and
  `remote_push_allowed=False`; requesting automatic remote push is refused.
  Actual deployment remains an explicitly separate executor concern.

## Evidence

`tests/test_remaining_selfmod_governance.py` covers guarded ordering,
worktree isolation and cleanliness metadata, failed verification and review,
evidence references, unrestricted bypass reporting, lifecycle completeness,
remote-push refusal, and intent idempotence.

Focused verification commands:

```text
python -m pytest -q tests/test_remaining_selfmod_governance.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python -m compileall -q sonder_runtime
git diff --check
```

No formal checklist checkboxes are changed by this slice.
