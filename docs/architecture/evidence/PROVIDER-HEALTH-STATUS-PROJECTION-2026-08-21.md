# Provider health status projection — 2026-08-21

## Scope

The live application graph now exposes a redacted provider-health projection
for the administrator operations dashboard. It reads only published typed
providers, serializes the stable provider id/status/detail/timestamp fields,
and converts a throwing health probe into an explicit `unhealthy` row rather
than hiding the provider or making the whole status response fail.

The HTTP `/v1/sonder/status` administrator payload includes this projection;
the existing restricted account payload remains unchanged and does not expose
host-wide provider state.

The same composition boundary now exposes cooperative provider cancellation.
Resolution honors the published scoped override, forwards only the typed
reason, and requires the provider to return a boolean activity result.

## Evidence

Focused commands:

```text
python -m pytest -q --basetemp .pytest-provider-health tests/production/test_composition_root.py tests/test_crosscutting_provider_lifecycle.py
```

The composition test proves the live graph publishes the embedding provider
through the stable redacted shape and exposes its cancellation port. The
lifecycle tests prove scoped cancellation resolution, typed health, and
cleanup behavior.

## Limitations

This remains `implemented_unverified`: it does not add a new provider backend,
training/update activation, external deployment receipt, or formal checklist
promotion. Health detail is limited to provider-owned safe text; endpoint and
credential data are not introduced by this projection.
