# WP3 SEAM-016: provider atomic lifecycle

`sonder_runtime.application.capabilities` provides the application-level
provider lifecycle boundary. A provider receives a private
`RegistrationScope`, initializes its resources, and stages all capabilities
there. The registry validates the complete staged surface and publishes the
provider only after initialization succeeds. Readers therefore see either the
previous registry or the complete new provider, never a partial provider.

If initialization or validation fails, the staged surface is discarded and
`shutdown()` is attempted exactly once. The registry remains unchanged. A
provider must expose a non-empty `provider_id`, `initialize(scope)`, and
`shutdown()`, and must register at least one non-null capability. Capability
names are unique within the registry.

The registry is an application service: its lock owns publication ordering;
provider objects own their resources. Provider callbacks must not call back
into the registry during initialization. This slice is provider-neutral and
does not modify gateway or tool modules.

## Evidence

- `tests/test_provider_lifecycle.py`
- `python -m pytest -q tests/test_provider_lifecycle.py`
- `python -m compileall -q sonder_runtime/application/capabilities`
