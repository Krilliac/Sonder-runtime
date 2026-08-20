# WP3 SEAM-015 — Scoped provider overrides

This slice adds the domain/application policy for an agent preset to replace a
provider within an explicit caller scope. It does not change provider
construction, initialization, shutdown, retries, or gateway dispatch.

## Contract

`ProviderOverridePolicy` is an immutable value containing the base provider
map and scoped replacements. `with_override` and `without_override` return new
policies; they never mutate a shared registry or process-global configuration.
The application `ProviderOverrideService` preserves that value-oriented
boundary for callers.

Resolution accepts a scope chain ordered from most specific to least specific.
The first matching `(scope, provider)` entry wins. If no entry matches, the
base provider is returned. Duplicate entries are rejected, and unknown base
providers fail closed, so results do not depend on registration or mapping
iteration order.

## Ownership and boundary

The domain module owns validation and deterministic resolution. The application
module owns caller-local policy composition. Provider instances and their
lifecycle remain outside this slice, as does `ModelGateway` or any other
gateway implementation.

Focused coverage is in `tests/test_provider_override_policy.py` and verifies
scope isolation, immutability/no global mutation, explicit precedence,
duplicate rejection, fail-closed lookup, and the application façade.
