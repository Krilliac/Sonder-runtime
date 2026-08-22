# Bounded provider-lifecycle composition slice — 2026-08-21

Status: implemented_unverified.

## Scope

This slice wires the existing typed lifecycle contracts into the live
composition boundary without entering session or job implementation lanes.
The application graph now owns a `ScopedProviderRegistry`, a scoped override
policy value, and the published specialized-provider bundle. The default local
embedding path is wrapped by `EmbeddingLifecycleAdapter` and passed to the
Ollama model gateway. Attended training and verified update backends can be
injected together; when absent, their providers are not published and resolve
fail-closed.

## Lifecycle evidence

- Typed publication and capability conflict handling remain atomic in
  `sonder_runtime/application/providers/lifecycle_registry.py`.
- Embedding, training, and update adapters expose typed health, cooperative
  cancellation, bounded cleanup, and immutable result normalization in
  `sonder_runtime/application/providers/specialized_lifecycle.py`.
- A failed multi-provider publication rolls back already-published providers.
- `Application.provider_health()` reports only published provider health, and
  `Application.close_providers()` provides the composition-owned cleanup
  boundary.
- The live Ollama model gateway consumes the typed embedding provider when the
  composition root supplies one; legacy module-shaped embedding providers remain
  compatible.

## Verification

Focused provider/lifecycle suite:

    python -m pytest tests/test_remaining_specialized_providers.py tests/test_crosscutting_provider_lifecycle.py -q

Result: 11 passed.

The composition-root suite could not be collected in this workspace because an
unrelated pre-existing import mismatch references `JobRecoveryReport` from
`sonder_runtime.application.jobs.durable_registry`, where that symbol is not
present. No session/job implementation files were changed to work around it.

## Limitations

- No concrete production training backend or update activator is selected by
  this slice; both remain explicit injection seams and are absent by default.
- Provider health is exposed on the typed `Application` object and projected
  into the administrator `/v1/sonder/status` payload; ordinary account status
  remains restricted from host-wide provider details.
- The existing model gateway has no general model-provider lifecycle port, so
  chat generation remains governed by its established gateway/context policy.
- Formal requirement promotion, deployment receipts, and full-repository tests
  remain outstanding.
