# Tier-Aware Local Provider Dispatch Design

**Status:** Approved in chat and reviewed on 2026-08-31
**Scope:** Sonder Runtime model-gateway composition for simultaneous local Ollama and Prism/llama.cpp inference

## Objective

Run the production Sonder hierarchy from one application graph without forcing every tier through one process-global inference backend:

- `fast` and `general` generation use the loopback Prism/llama.cpp OpenAI-compatible endpoint and Ternary Bonsai.
- `code` and `reasoning` generation remain on local Ollama and the configured Qwen model.
- Embeddings remain on local Ollama and the configured embedding model.
- Existing single-provider installations keep their current behavior when no per-tier binding is configured.

This slice changes transport selection only. It does not install models, start or supervise Prism, add token-level speculative decoding, change runtime-policy model names, or introduce cloud fallback.

## Current State and Gap

`ModelGateway` already provides a common `generate`/`embed` boundary. `OllamaGateway` and `OpenAICompatibleGateway` already implement it, including deadline handling, response validation, telemetry normalization, and endpoint consent checks.

The composition roots currently choose exactly one gateway from `SONDER_MODEL_BACKEND`. `ModelRoute` carries a provider name, but `AvailableModels` supplies one provider for every tier. Consequently, one Sonder process cannot send Bonsai traffic to Prism while keeping Qwen and embeddings on Ollama.

## Chosen Architecture

Add a small composite `ProviderDispatchGateway` behind the existing `ModelGateway` port. It owns:

1. A fixed mapping of canonical provider names to gateway instances.
2. A fixed mapping of model tiers to canonical provider names.
3. One explicitly configured embedding provider.

For generation, the dispatcher selects exactly one provider from the request tier and invokes it once. For embeddings, it invokes the configured embedding provider. It never retries through another provider. Existing adapters retain ownership of endpoint consent, deadlines, transport errors, request construction, and telemetry.

The dispatcher is an adapter/composition concern. Provider bindings do not enter the hot-reloadable runtime-policy document because that document intentionally holds model aliases and routing lanes, not endpoints, credentials, or cloud consent.

## Configuration Contract

`SONDER_MODEL_BACKEND` remains the default generation provider and defaults to `ollama`. Existing accepted aliases remain valid:

- `ollama` normalizes to `ollama`.
- `openai`, `openai-compatible`, `llamacpp`, and `vllm` normalize to `openai_compatible`.

Optional tier overrides use these environment variables:

- `SONDER_FAST_PROVIDER`
- `SONDER_GENERAL_PROVIDER`
- `SONDER_CODE_PROVIDER`
- `SONDER_REASONING_PROVIDER`
- `SONDER_VISION_PROVIDER`

`SONDER_EMBEDDING_PROVIDER` selects the embedding provider. When it is unset or blank, it inherits the normalized `SONDER_MODEL_BACKEND` so existing single-provider OpenAI-compatible installations remain direct and unchanged. The production mixed Sonder profile explicitly sets `SONDER_EMBEDDING_PROVIDER=ollama`.

An unset or blank tier override inherits the normalized `SONDER_MODEL_BACKEND`. Any nonblank unknown provider value is a startup configuration error. The composition root creates only providers referenced by a generation or embedding binding. OpenAI-compatible endpoint and model configuration continue to use `SONDER_OPENAI_BASE_URL`, `SONDER_OPENAI_MODEL`, `SONDER_OPENAI_API_KEY`, and `SONDER_OPENAI_EMBED_MODEL`.

The intended production bindings are:

```text
SONDER_MODEL_BACKEND=ollama
SONDER_FAST_PROVIDER=openai-compatible
SONDER_GENERAL_PROVIDER=openai-compatible
SONDER_CODE_PROVIDER=ollama
SONDER_REASONING_PROVIDER=ollama
SONDER_EMBEDDING_PROVIDER=ollama
SONDER_OPENAI_BASE_URL=http://127.0.0.1:<prism-port>
SONDER_OPENAI_MODEL=<Prism server model identifier>
```

The port remains loopback-only in the production profile unless an existing operation context explicitly grants cloud consent; the OpenAI-compatible adapter continues enforcing that rule.

## Components and Responsibilities

### Provider binding parser

A focused bootstrap helper normalizes aliases and builds an immutable binding object containing:

- `default_generation_provider: str`
- `tier_providers: Mapping[str, str]`
- `embedding_provider: str`

It validates all configured names before any model call. Both composition roots consume the same helper so CLI/HTTP/MCP entry points cannot drift.

### ProviderDispatchGateway

The dispatcher implements the existing `ModelGateway` protocol:

- `generate(request, context)` requires a configured provider for the request tier, forwards the original request and context unchanged, and returns the selected gateway response unchanged.
- `embed(texts, context)` forwards to the single configured embedding gateway.
- Missing bindings and missing provider instances fail closed with `InvalidInput` during construction or request validation; they do not silently use a different transport.

The class contains no network logic, model-name rewriting, retry loop, policy mutation, or health probing.

### Composition roots

`bootstrap/app.py` and `bootstrap/container.py` construct the normalized bindings, instantiate the referenced gateway adapters, and wrap them with `ProviderDispatchGateway` only when dispatch is required. A fully single-provider configuration may return the direct gateway to preserve the simplest existing object graph and compatibility tests.

### Routing metadata

`AvailableModels` gains a per-tier provider mapping while retaining a default provider for compatibility. `RoutePlanner` records the selected tier's provider in `ModelRoute`. This makes route inspection agree with the gateway that will execute the request.

### Read-only status

The composition layer exposes a bounded, content-free provider projection containing the default generation provider, each local tier binding, and the embedding provider. Status must not include endpoint URLs, API keys, prompt content, model output, or filesystem paths. It reports configuration, not inferred server health; live endpoint reachability remains the adapter/runtime health concern.

## Data Flow

1. Startup reads and validates provider bindings.
2. The composition root creates the required Ollama and/or OpenAI-compatible gateways.
3. The route planner selects a tier and records that tier's provider.
4. `ChatService` creates its existing `ModelRequest`.
5. `ProviderDispatchGateway.generate` selects the bound gateway from `request.tier` and forwards the same request/context once.
6. The selected adapter performs consent checks and inference, records its existing backend telemetry, and returns `ModelResponse`.
7. Embedding calls bypass generation-tier bindings and always use the configured embedding provider.

## Failure and Safety Semantics

- Unknown provider names fail at composition time with a bounded configuration error.
- Unknown/unbound request tiers fail closed; they do not inherit an arbitrary neighbor tier.
- A provider transport failure is returned unchanged through the domain error taxonomy.
- There is no cross-provider retry or failover.
- Non-loopback OpenAI-compatible endpoints still require explicit cloud consent.
- Remote Ollama endpoints still require explicit remote-Ollama consent.
- Provider status is content-free and does not expose endpoint or credential material.
- Existing model files, Ollama manifests, and runtime-policy documents are not moved, deleted, or rewritten by this feature.

## Testing Strategy

Use strict red-green TDD with behavior-level tests:

1. Dispatcher tests prove fast/general and code/reasoning reach different real fake gateways, embeddings use the independent embedding binding, requests/contexts are forwarded unchanged, and provider failures never invoke a second gateway.
2. Configuration tests prove alias normalization, tier and embedding inheritance from `SONDER_MODEL_BACKEND`, the explicit Ollama embedding override used by the mixed profile, and rejection of unknown nonblank values.
3. Route-planner tests prove `ModelRoute.provider` follows the selected tier rather than one global provider.
4. Composition tests prove the default graph remains direct Ollama, the legacy global OpenAI-compatible mode remains direct, and a mixed binding builds the dispatcher with the expected content-free projection.
5. Existing gateway conformance, consent, runtime-policy, telemetry, architecture, and full unit suites remain green.
6. A loopback integration smoke, after Prism is installed and Bonsai is verified, proves `fast` executes through Prism while `code` and embeddings still execute through Ollama.

## Non-Goals and Follow-Up Slices

The following remain separate, independently reviewable work:

- Prism process supervision and automatic model loading.
- llama.cpp KV-cache type and prompt/prefix-cache tuning.
- Token-level n-gram, MTP, DFlash, or DSpark speculative decoding.
- Generalized cross-provider benchmark tournaments and promotion history.
- Model/provider-specific metrics labels beyond the existing bounded backend labels.
- Automatic provider failover or cloud fallback.

Keeping those out of this slice makes the dispatch boundary small enough to verify without coupling runtime orchestration, performance policy, and evaluation policy.

## Acceptance Criteria

The design is implemented when all of the following are proven:

1. One application graph can bind `fast`/`general` to OpenAI-compatible Prism and `code`/`reasoning` plus embeddings to Ollama.
2. Existing default Ollama and global OpenAI-compatible configurations remain compatible.
3. `ModelRoute.provider` matches the executing tier binding.
4. Invalid provider configuration fails closed before inference.
5. Provider failure never triggers an implicit second-provider call.
6. Provider status contains bindings only and no endpoint, secret, content, output, or path data.
7. Targeted tests, architecture checks, and the full relevant unit suite pass.
8. The final live smoke demonstrates the intended Prism/Ollama split after the verified model/runtime pipeline is ready.
