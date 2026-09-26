# Sonder Inference provider (`sonder_inference`)

Reference for the ModelGateway provider that sends generation to a
`sonder-infer serve` process (Sonder Inference HTTP API v1). Decision record:
[ADR-2026-09-26-sonder-inference-provider](../adr/ADR-2026-09-26-sonder-inference-provider.md).
Operator procedure: [runbook](../runbooks/sonder-inference.md).
Verified against the code on 2026-09-26.

## Code map

| Concern | Module |
|---|---|
| Aliases, `ProviderBindings.fallbacks`, label-to-id map | `sonder_runtime/adapters/provider_bindings.py` |
| Gateway, config, consent, health, identity, status | `sonder_runtime/adapters/inference/sonder_inference_gateway.py` |
| Shared HTTP transport and its additive hooks | `sonder_runtime/adapters/inference/openai_compat_gateway.py` |
| Fail-closed fallback | `sonder_runtime/adapters/provider_dispatch/fallback.py` |
| Composition | `sonder_runtime/adapters/model_gateway_factory.py`, `adapters/runtime_container.py` |
| Status aggregation | `ProviderDispatchGateway.provider_status()` in `adapters/provider_dispatch/gateway.py` |
| Identity reader and protocol probe | `sonder_runtime/adapters/inference/sonder_inference_probe.py`, `scripts/backend_attest.py --backend sonder-inference` |
| Doctor and preflight | `sonder_doctor.py` (`sonder_inference`, `sonder_inference_scope`), `adapters/preflight.py` |

## Selecting the provider

`sonder-inference`, `sonder_inference`, `sonder-infer` and `inference` all
normalize to `sonder_inference`, in `SONDER_MODEL_BACKEND` and in every
`SONDER_<TIER>_PROVIDER` / `SONDER_EMBEDDING_PROVIDER` binding. `sonder` is
refused as an unknown provider because it names the chat tier and the local
model alias; the error points at `sonder-inference`.

Inference API v1 serves no embeddings, so a deployment that binds generation
to Inference binds embeddings elsewhere:

```text
SONDER_MODEL_BACKEND=sonder-inference
SONDER_EMBEDDING_PROVIDER=ollama
```

An embedding call routed to Inference raises `DependencyUnavailable` telling
the operator to set `SONDER_EMBEDDING_PROVIDER=ollama`.

## Configuration

Read lazily on every call (like `SONDER_OPENAI_*`); nothing is read at import
or construction.

| Variable | Default | Meaning |
|---|---|---|
| `SONDER_INFERENCE_BASE_URL` | `http://127.0.0.1:11437` | Server origin (a path prefix is allowed for a proxy). `0.0.0.0`/`::` are rewritten to `127.0.0.1`/`::1`. |
| `SONDER_INFERENCE_READY_FILE` | unset | Used only when the base URL is unset: the `url` field of a `serve --ready-file`. A missing file means the server is not listening yet and raises `SonderInferenceUnreachable`; a malformed file is a configuration error. |
| `SONDER_INFERENCE_MODEL` | `default` | Model id sent when no tier mapping applies. |
| `SONDER_INFERENCE_TIER_MODELS` | unset | `fast=a,general=b`; keys must be provider tiers. |
| `SONDER_INFERENCE_API_KEY` | unset | Sent as `Authorization: Bearer`; in the log redaction set. |
| `SONDER_ALLOW_REMOTE_INFERENCE` | `0` | `1` permits a non-loopback base URL (see consent). |
| `SONDER_INFERENCE_TIMEOUT_SECONDS` | `300` | Per-call ceiling, never beyond the operation deadline. |
| `SONDER_INFERENCE_HEALTH_TTL_SECONDS` | `5` | How long a health observation is reused. |
| `SONDER_INFERENCE_FALLBACK` | `none` | `none` or `ollama`; anything else fails composition. |

Base URL resolution order: `SONDER_INFERENCE_BASE_URL`, then the ready file,
then the default. Invalid values (unknown tier keys, non-numeric timeouts,
`SONDER_ALLOW_REMOTE_INFERENCE` other than `0`/`1`) raise `InvalidInput`.

## Consent

- Loopback (`127.0.0.0/8`, `localhost`, `::1`) needs nothing.
- Any other host is refused with `Forbidden` before any byte is sent unless
  `SONDER_ALLOW_REMOTE_INFERENCE=1`, the URL is `https://`, an API key is set,
  and (for prompt-bearing calls) the `OperationContext` has `cloud_allowed`.
  The context flag keeps surfaces such as A2A, whose contexts do not allow
  cloud, from sending prompts off-host through an env-only opt-in.
- Health and identity probes apply the URL, TLS and key checks (they carry
  the key) but not the context flag (they carry no prompt).

## Wire format

`POST /v1/chat/completions` with the OpenAI subset: `model`, `messages`
(system, history, user), `stream: false`, and only the sampling options the
caller set. Ollama option names map as `num_predict`→`max_tokens`; `temperature`,
`top_p`, `top_k`, `min_p`, `typical_p`, `seed`, `stop` (string or up to four),
`presence_penalty`, `frequency_penalty`, `repeat_penalty`, `repeat_last_n` and
`num_ctx` pass through by name (the last four as Sonder extensions). `format`,
`tools`, `tool_choice`, `functions`, `response_format` and `think=True` are
refused locally with `InvalidInput`; Inference v1 would reject them anyway.

Model selection: an explicit `ModelRequest.options["model"]`, else
`SONDER_INFERENCE_TIER_MODELS[tier]`, else `SONDER_INFERENCE_MODEL`. The
runtime policy's Ollama model names are never forwarded.

`ModelResponse.model` is the response's `model` field (the resolved id).
A response that omits it or answers `default` is refused as an incompatible
API. Usage comes from `usage`, falling back to `timings.prompt_n` /
`predicted_n`; `ModelResponse.telemetry` comes from `from_openai_compatible`.

Headers per call:

| Header | Value |
|---|---|
| `X-Sonder-Parent-Request-Id`, `X-Sonder-Run-Id` | `context.correlation_id`, omitted unless it matches `[A-Za-z0-9._:-]{1,128}` |
| `X-Sonder-Workload` | `http`/`repl` → `interactive_user`, `mcp` → `owner_orchestrator`, `worker` → `implementation_worker`, `system` → `maintenance` |
| `Authorization` | `Bearer <key>` only when a key is set; extra headers can never replace it |

The capture label passed to `dispatch_provider` is `sonder-inference`.
`provider_bindings.PROVIDER_LABEL_IDS` maps labels to provider ids
(`ollama`→`ollama`, `openai-compatible`→`openai_compatible`,
`sonder-inference`→`sonder_inference`); the existing labels are persisted
capture evidence and do not change.

## Errors and "provably not executed"

`SonderInferenceUnreachable` (a `DependencyUnavailable` subclass that keeps
the `DEPENDENCY_UNAVAILABLE` code, so session capture records it like any
provider outage) is raised only when the request provably did not run:

1. TCP connection refused;
2. host name does not resolve;
3. cached health is not `ready` (including a missing ready file);
4. HTTP 503 with error code `not_ready`.

Everything else maps like the OpenAI-compatible gateway and is never
"unreachable": 400/404/405/411/413/501 → `InvalidInput`; 401/403 →
`Forbidden`; 429 and 503 `overloaded` → `CapacityExceeded`; 408, 500,
503 `backend_unavailable` and connection resets → `DependencyUnavailable`;
socket timeouts → `DeadlineExceeded`. Calls are single-attempt, and deadline
and cancellation are checked before the health probe, before the send and
after the response.

Before each send the gateway consults cached health (`GET /v1/sonder/health`,
timeout at most 2 s, reused for the TTL). The API major version must be 1,
read from the health document and, when present, from the response body's
`sonder.api_version` (the shared transport cannot read response headers). A
mismatch raises plain `DependencyUnavailable` ("incompatible sonder-inference
API"), which never triggers the fallback. Rejected credentials on health raise
`Forbidden`.

With Inference down and no fallback, the error names the base URL,
`sonder-infer serve` and `SONDER_INFERENCE_FALLBACK`.

## Fallback (fails closed)

`SONDER_INFERENCE_FALLBACK=ollama` is valid only while a generation tier or
the default is bound to `sonder_inference`; otherwise composition fails.
`build_model_gateway` constructs the Ollama gateway once and wraps the
Inference gateway in `PreSendFallbackGateway`:

- On `SonderInferenceUnreachable` only, the same `ModelRequest` goes to Ollama
  once, after re-checking cancellation and deadline. The fallback is counted
  (`fallback_count`) and logged at WARNING with the correlation id.
- Timeouts, 4xx, other 5xx, cancellation and capacity refusals propagate
  unchanged: no retry, no double execution.
- Ollama keeps its own consent rules, so the fallback never reaches the cloud.
- The fallback target is not a routable binding: `ProviderBindings.required_providers`
  includes it (so it is constructed), `ProviderBindings.bound_providers` does
  not, and tier dispatch only sees the wrapper.
- `build_model_gateway(..., fallback_observer=...)` receives
  `(from_provider, to_provider, reason_code, context)` once per fallback,
  before the fallback send. This is the seam for a `route.changed` event,
  because a refusal from cached health never reaches `dispatch_provider`.
- Identity-bound runtimes (`build_runtime(route_evidence=...)`) refuse a
  fallback configuration.

## Health, identity and status

These never generate.

- `capability_health()` → `CapabilityHealth(provider="sonder_inference",
  capabilities={GENERATION}, healthy=<state is ready>, checked_at, detail)`.
- `backend_identity(model=None)` → `BackendIdentity | None` from
  `GET /v1/sonder/identity[?model=]` (schema `sonder.inference.identity/1`).
  `backend_identity` must carry exactly the nine `BackendIdentity` keys with
  lowercase 64-hex digests; anything else is refused. `null` stays `None`
  with Inference's reason (`observe_identity()` exposes it with the
  `synthetic` flag).
- `routing_identity(model=None)` returns `None` for a synthetic identity:
  mock identities are shown but never satisfy identity-bound routing, and
  `backend_attest.py` never records one.
- `provider_status()` → `{"sonder_inference": {...}}` with exactly these keys:
  `provider`, `state` (`ready` | `degraded` | `unavailable`), `healthy`,
  `checked_at` (RFC 3339 with milliseconds and `Z`, or null), `detail`
  (at most 240 characters), `capabilities` (list), `base_url` (the loopback
  URL, or scheme and host only when remote), `version`, `api_version`,
  `models` (ids), `synthetic` (bool or null), `identity` (the nine keys or
  null), `telemetry` (`discovery_url`, `sse_url`, `ndjson_url` as absolute
  URLs, or null when unreachable), `fallback`, `fallback_count`.
  `PreSendFallbackGateway` fills in `fallback`/`fallback_count`, and
  `ProviderDispatchGateway.provider_status()` aggregates every provider,
  reporting providers without the method as `{"provider": id, "state": "unknown"}`.

## Doctor, preflight and discovery

- `python -m sonder_runtime doctor` runs `sonder_inference` (skipped when no
  binding uses it; ok when ready; warn for the mock backend or for an outage
  that `SONDER_INFERENCE_FALLBACK=ollama` covers; fail when bound and
  unreachable without a fallback, on an API version mismatch, or on invalid
  bindings) and `sonder_inference_scope` (warn when bound: REPL, MCP,
  autopilot and fleet generate through the legacy Ollama path regardless of
  bindings). `--skip-inference` removes both.
- `serve`/`preflight` add a non-required `sonder_inference` check only; startup
  never blocks on Inference.
- `environment_probe` lists `sonder-infer` as a specialist tool and
  `toolchain_policy` allows the fixed argument `version` for it.

## Which surfaces use the provider

Provider bindings are honoured by ModelGateway consumers: `ChatService`
(`POST /a2a` SendMessage) and session summarize/title offload. On this
revision, `POST /v1/chat/completions` still runs the legacy Ollama chat path
(`server.py` `_chat_request`); routing it through the gateway is contract
section 4, owned by the chat-telemetry lane (its bridge module is
`application/chat/provider_bridge.py` on that branch). REPL, MCP, autopilot
and fleet generate through the legacy Ollama path regardless of bindings, which
the `sonder_inference_scope` doctor check states. The `/v1/models` listing and
the escalation rungs deduplicate by Ollama model name, which carries no
meaning for Inference-bound tiers.

## Open questions (cross-repo shapes not pinned by the contract)

- open: `backend_identity.backend` for an Inference deployment names the
  engine backend (`mock`, `ollama`, `llamacpp`). `backend_attest.py` keys
  evidence by that value because `BackendConformanceRecord` requires the
  identity and record backends to match, so Inference-over-Ollama evidence
  shares the `ollama` key with direct Ollama evidence. Owner: Inference
  `docs/SERVER.md` plus this document.
- open: the protocol probe sends `identity.model` as the request model, which
  assumes Inference reports the served model id as `backend_identity.model`.
- open: whether Inference includes `sonder.api_version` in every JSON body
  (the critique's correction); the gateway checks it when present and relies
  on the health document otherwise.
- open: the bootstrap composition root computes
  `strict_alias_provider_available` from `required_providers`, which now
  includes a fallback target; the dispatcher only sees bound providers, so a
  strict-alias request with a fallback-only Ollama fails with `InvalidInput`
  instead of reaching Ollama. Switching bootstrap to `bound_providers` belongs
  to the bootstrap owner.
- open: the `fallback_observer` seam is not wired by bootstrap on this
  branch; emitting `route.changed` from it belongs to the chat-telemetry lane.
- open: SECURITY.md rows for the telemetry and ecosystem routes (contract
  section 13) wait for those routes to exist on the integrated branch; the
  proposed text is in the ADR.
