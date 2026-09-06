# Memory retention and deletion gate

`sonder_runtime.domain.memory.retention_gate` is a pure, provider-neutral
contract for deciding whether a storage adapter may remove one versioned memory
record. It accepts a project-scoped `MemoryRecordIdentity` containing the
record version and tombstone identity, an explicit `MemoryRetentionPolicy`,
active job and deployment references, and acknowledgements from backups and
replicas.

The decision is fail-closed:

- no explicit expiry (`retain_until`) means indefinite retention;
- any matching active job or deployment reference blocks deletion;
- evidence from another project, record version, or tombstone blocks deletion;
- the default policy requires one matching backup acknowledgement and one
  distinct replica holder;
- a single-PC profile can explicitly set the required replica count to zero
  while still requiring its backup acknowledgement.

Replica acknowledgements count distinct `holder_id` values, so repeated
messages from one holder cannot satisfy an independence requirement. Reason
codes and the human-readable explanation are bounded and contain no record
content. `MemoryRetentionDecision.as_dict()` preserves the exact target
identity and evidence counts for an operator-facing receipt.

This slice deliberately does not delete a database row or file, copy data,
contact a provider, mutate a tombstone, or implement consensus. An adapter
must revalidate the same identity and evidence immediately before its own
side-effect boundary. A successful pure decision is therefore eligibility
evidence, not proof that deletion has occurred.

Focused evidence: `tests/test_memory_retention_gate.py`. The suite covers
expiry, version/tombstone binding, project scope, active references, backup and
replica acknowledgements, distinct-holder counting, single-PC policy, bounded
explanations, validation, and serialization.

Verification on 2026-09-05 (branch `codex/p5-memory-retention-gate`):

- `pytest -q tests/test_memory_retention_gate.py` — 14 passed;
- the focused memory, governance, store, tools, and session-privacy suites —
  136 passed;
- `python -m compileall -q sonder_runtime` — passed;
- `python scripts/check_architecture.py` — passed;
- `git diff --check` — passed.
