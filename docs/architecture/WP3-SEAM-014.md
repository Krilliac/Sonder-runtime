# WP3 SEAM-014 — Specialized lifecycle ports

This slice adds the provider-neutral application port module
`sonder_runtime.application.ports.specialized_lifecycle` with typed contracts
for `EmbeddingProvider`, `TrainingBackend`, and `UpdateActivator`.

Each contract exposes an immutable health snapshot, cooperative cancellation,
and idempotent bounded cleanup that reports whether the provider reached
quiescence and released its resources. Operations receive the existing
`OperationContext`, so deadlines and cancellation remain explicit at the
boundary. Embedding, deployment, and activation outputs are frozen dataclasses;
deployment and activation results carry immutable identity and digest evidence
instead of mutable adapter state or paths.

This is an additive port-only change. Existing embedding, training, and update
adapters are not modified or wired. Provider-specific validation, scheduling,
process supervision, verification, rollback, and resource ownership remain
adapter responsibilities.

## Verification

```text
python -m pytest -q tests/test_wp3_seam014_contracts.py
python -m compileall -q sonder_runtime/application/ports/specialized_lifecycle.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
git diff --check
```

No specification checkbox, evidence ledger, commit, or push is part of this
slice.
