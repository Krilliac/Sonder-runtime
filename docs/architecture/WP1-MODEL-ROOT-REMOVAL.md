# WP1 model-adapter root removal

## Boundary

`legacy_model_gateway.py` and `ollama/gateway.py` no longer import the flat
`server` composition root.  The adapters depend only on application ports,
domain errors, platform policy, and provider-owned adapter dependencies.

## Injected contracts

`sonder_runtime.application.ports.model_target` defines the root-free
`ModelTarget`, `ModelTargetResolver`, `ModelSystemBuilder`, and
`ModelGenerateFactory` contracts.  The application composition boundary owns
tier selection and system-context composition; the provider adapter owns the
transport call after those decisions are supplied.

The legacy gateway accepts explicit `generate` and `embed` providers.  Ollama
accepts explicit target resolution, system composition, generation-factory,
and embedding dependencies.  If required chat dependencies are absent, the
gateways fail closed with `DependencyUnavailable` instead of discovering or
importing a root module.  Existing provider-owned embedding defaults remain
available for compatibility.

## Evidence

`tests/test_wp1_model_adapter_root_removal.py` verifies the absence of direct
server imports and preserves prompt/history/tier, system, options, context
size, usage, and embedding behavior through injected dependencies.  The
formal master-spec checkboxes are intentionally unchanged.
