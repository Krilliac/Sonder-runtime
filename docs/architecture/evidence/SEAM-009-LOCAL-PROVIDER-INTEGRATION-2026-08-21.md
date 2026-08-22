# SEAM-009 — local provider integration evidence

This bounded slice connects the typed `SubagentProvider` contract to the
durable local continuation runner through `LocalSubagentProvider`.

## Implemented boundary

- `LocalSubagentProvider` is the named local provider adapter and rejects
  unsupported provider names before child publication.
- `register_root` publishes a durable parent admission anchor. Spawning an
  unknown parent fails closed, so every child has an explicit provider-owned
  lineage.
- The adapter preserves parent/child identity, nested lineage, cooperative
  cancellation, and first-reason-wins cancellation semantics.
- Checkpoint writes and output are bounded by the typed child budget. Terminal
  failures are represented as structured timed-out results rather than false
  success.
- `DelegationService.integrate` consumes the real local result and emits the
  bounded digest-backed `ResultEvidence` envelope.

## Verification

```text
python -m pytest -q tests/test_seam009_local_provider.py
python -m compileall -q sonder_runtime tests/test_seam009_local_provider.py
python scripts/check_architecture.py
python scripts/check_evidence_documents.py
git diff --check
```

The separately deployed external-provider acceptance path remains outside this
local adapter and is intentionally unsupported/fail-closed in this slice.
