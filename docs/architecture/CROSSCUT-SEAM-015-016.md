# Cross-cutting provider lifecycle registry — SEAM-015/016

This slice adds `ScopedProviderRegistry` as the application-owned lifecycle
service for the WP3 capability seams. It composes the existing
`HealthReport`, `CleanupResult`, and specialized lifecycle ports; it does not
replace or mutate those port definitions.

## Contract

- Providers stage capabilities through a private, single-use scope.
- Initialization failures invoke bounded cleanup and publish nothing.
- Capability conflicts and duplicate registrations leave the prior snapshot
  unchanged.
- Scoped overrides are explicit immutable `(scope, base, replacement)` rules.
  They affect only resolution for callers that provide the matching scope.
- Health is observed from the resolved provider and cleanup must report both
  quiescence and resource release before unpublication.
- Registry reads return stable snapshots; lifecycle operations are serialized.

## Ownership and thread boundary

The registry owns publication metadata and override rules. Providers own their
resources and implement initialization, health, and cleanup. Registry methods
are thread-safe; provider callbacks must not re-enter the registry during
initialization or cleanup. Provider work remains provider-owned and async-safe.

Focused coverage is in `tests/test_crosscutting_provider_lifecycle.py`.
