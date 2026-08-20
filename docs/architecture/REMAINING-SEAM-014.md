# Remaining SEAM-014 — Specialized provider lifecycle wiring

## Scope

This slice closes the implementation gap identified by `REQUIREMENT-AUDIT-NEXT`:
the typed `EmbeddingProvider`, `TrainingBackend`, and `UpdateActivator` ports
existed, but concrete providers were not published through the lifecycle
registry.

## Contract

`sonder_runtime.application.providers.specialized_lifecycle` supplies injected
adapters for all three ports. Each adapter publishes one capability through
`ScopedProviderRegistry`, exposes health/cancel/cleanup, rejects work after
closure, wraps the caller cancellation token, and waits for active operations
to quiesce before reporting resources released. Delegates are injected so the
composition root remains the only place that chooses Ollama, training, or
update implementations; this slice has no network or legacy-root imports.

`wire_specialized_providers` publishes embedding, training, and update adapters
as one composition operation. If a later registration fails, already-published
registrations are synchronously unregistered and the registry returns to its
pre-call state. `SpecializedProviderBundle.close` removes the published
providers in reverse order.

## Evidence

- `tests/test_remaining_specialized_providers.py`
- Focused tests cover capability publication, typed result normalization,
  request validation, cancellation propagation, bounded cleanup, and rollback
  of partial publication.
- No formal checklist edits are included in this slice.

## Verification

```text
python -m pytest -q tests/test_remaining_specialized_providers.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python -m compileall -q sonder_runtime
git diff --check
```
