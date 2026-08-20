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

The original port-only slice is now complemented by
`sonder_runtime.application.providers.specialized_lifecycle`. That module
provides injected lifecycle adapters for the three declared surfaces and the
atomic `wire_specialized_providers` composition boundary. Publication is
fail-closed: a partial registration is rolled back, and registry resolution
cannot produce an absent provider. Provider-specific validation, scheduling,
process supervision, verification, rollback, and resource ownership remain
adapter responsibilities.

Implementation evidence is maintained in
[`REMAINING-SEAM-014.md`](REMAINING-SEAM-014.md).

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
