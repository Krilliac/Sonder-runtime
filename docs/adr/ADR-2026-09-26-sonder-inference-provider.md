# ADR 2026-09-26: Sonder Inference as a ModelGateway provider

**Status:** Accepted (the remote-inference consent rule is a security-posture
addition and is recorded here and in `SECURITY.md` for owner review)
**Date:** 2026-09-26
**Context:** Sonder ecosystem integration contract v1, section 3;
`sonder_runtime/adapters/inference/sonder_inference_gateway.py`,
`sonder_runtime/adapters/provider_dispatch/fallback.py`,
`sonder_runtime/adapters/provider_bindings.py`

## Decision

Sonder Inference is reached over local HTTP (`sonder-infer serve`, Inference
ADR-020, API version 1) as a new provider id, `sonder_inference`, selected by
the existing provider bindings. `SonderInferenceGateway` reuses the
`OpenAICompatibleGateway` transport through additive hooks (provider label,
per-call headers, bounded error-body and connect-failure classifiers, a GET
helper); no second HTTP client exists. An optional, fail-closed
`PreSendFallbackGateway` sends a request to local Ollama only when Inference
provably never executed it. The reference is
[sonder-inference-provider.md](../architecture/sonder-inference-provider.md).

## Rationale

- **HTTP rather than in-process ctypes.** A native crash must not take the
  Python runtime down, the same listener serves live telemetry, and the
  runtime already has an OpenAI-compatible transport with a tested consent
  gate and error taxonomy.
- **Reuse the transport, add hooks.** The OpenAI-compatible transport cannot
  see response headers or error bodies. Rather than a second client, the
  hooks let the Sonder adapter read a bounded error body (to tell 503
  `not_ready` from 503 `backend_unavailable`) and classify refused or
  unresolvable connections. Default hook values keep every existing caller's
  behaviour and capture label (`openai-compatible`) unchanged.
- **"Unreachable" is narrow on purpose.** Fallback is only safe when the
  request did not run: connection refused, unresolvable host, cached health
  not ready, or 503 `not_ready`. A timeout or 5xx may have executed, so
  replaying it on Ollama would double effects and cost. The subclass keeps
  the `DEPENDENCY_UNAVAILABLE` code because session capture rejects unknown
  failure codes.
- **Consent needs both env and context.** An env-only opt-in would let
  surfaces whose operation context forbids cloud (A2A) send prompts
  off-host. Remote endpoints therefore need `SONDER_ALLOW_REMOTE_INFERENCE=1`,
  `https://`, an API key, and a cloud-allowed context.
- **`sonder` is not an alias.** It names the chat tier and the local model
  alias; accepting it as a provider would turn a tier typo into a transport
  change.
- **Synthetic identities are display-only.** The mock backend reports
  well-formed digests; `routing_identity()` and the attestation CLI refuse
  them so mock output can never become routing evidence.

## Consequences

- `ProviderBindings` gains `fallbacks` (only `sonder_inference -> ollama`) and
  `bound_providers`; `required_providers` includes fallback targets, and
  `status_projection()` reports `fallbacks`.
- `ProviderDispatchGateway` aggregates `provider_status()` and passes
  `capability_health()` through.
- Doctor gains `sonder_inference` and `sonder_inference_scope`
  (`--skip-inference`), preflight gains a non-required note, and
  `backend_attest.py` accepts `--backend sonder-inference`.
- `SONDER_INFERENCE_API_KEY` joins the log redaction set.
- Identity-bound runtimes refuse a fallback configuration.
- Ruled out: streaming through the gateway, embeddings via Inference, runtime
  supervision of `sonder-infer` processes, and any fallback other than
  Inference to local Ollama.

## Proposed SECURITY.md rows for the integrator

The telemetry and ecosystem routes (contract sections 5 and 9) are
implemented by the chat-telemetry lane, not on this branch. Once they are on
the integrated branch, `SECURITY.md` should carry rows stating that
`/.well-known/sonder-telemetry`, `/v1/observability/events` and
`/v1/sonder/ecosystem` require admin authorization exactly as
`/v1/observability/trace` does, export content-free events only, and apply
the exact-match CORS allowlist.
