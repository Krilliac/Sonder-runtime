# SEC-009 — Guarded recovery and audit-file evidence

## Scope

`sonder_runtime/application/security/recovery_artifacts.py` adds a concrete,
filesystem-backed recovery artifact service without changing the formal master
checklist or the requirement audit.

The service:

- requires an absolute, non-link root and validates every generated artifact
  path stays beneath that root;
- bounds artifact identifiers, payload size, and audit-entry count;
- requires the application actor identity to match the configured owner;
- writes the payload, owner metadata, digest, and previous-audit reference;
- maintains a deterministic SHA-256 audit chain; and
- fails closed when payload, metadata, ownership, or chain verification fails.

## Security limitation

This is tamper-evident evidence, not tamper-resistant storage.  The owner and
actor labels are application identities, not operating-system credentials.  A
same-user process with write access can replace the payload, metadata, and
audit file together and recompute their digests.  Therefore the service and
its callers must never claim same-user security, immutable audit, or an
independent recovery authority.  A separately enforced owner, append-only
store, remote auditor, or cryptographic signing key outside the actor's write
authority is required for that stronger property.

## Verification

`tests/test_remaining_sec_009.py` proves:

- normal write/inspect/verify behavior and chained audit continuity;
- actor, identifier, root, and payload bounds;
- detection of changed payloads and changed audit entries; and
- the explicit `tamper_evident_only` limitation.

Formal checklist edits are intentionally out of scope.
