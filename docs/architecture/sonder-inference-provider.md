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
| Doctor and preflight | `sonder_doctor.py` (`sonder_inference`, `sonder_inference_scope`, `sonder_inference_gpu`), `adapters/preflight.py` |
| Streaming, thinking, sampling defaults, residency | see [request path](../integration/sonder-inference-request-path.md) |

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

Provider configuration is read lazily on every call (like `SONDER_OPENAI_*`).
Agent generation controls below are frozen at generator construction so a
task does not switch thinking or sampling modes between decisions.

| Variable | Default | Meaning |
|---|---|---|
| `SONDER_INFERENCE_BASE_URL` | `http://127.0.0.1:11437` | Server origin (a path prefix is allowed for a proxy). `0.0.0.0`/`::` are rewritten to `127.0.0.1`/`::1`. |
| `SONDER_INFERENCE_READY_FILE` | unset | Used only when the base URL is unset: the `url` field of a `serve --ready-file`. A missing file means the server is not listening yet and raises `SonderInferenceUnreachable`; a malformed file is a configuration error. |
| `SONDER_INFERENCE_MODEL` | `default` | Model id sent when no tier mapping applies. |
| `SONDER_INFERENCE_TIER_MODELS` | unset | `fast=a,general=b`; keys must be provider tiers. |
| `SONDER_INFERENCE_API_KEY` | unset | Sent as `Authorization: Bearer`; in the log redaction set. |
| `SONDER_ALLOW_REMOTE_INFERENCE` | `0` | `1` permits a non-loopback base URL (see consent). |
| `SONDER_INFERENCE_MAX_INFLIGHT` | `1` | The primary's parallel capacity for private-worker placement (1-16); unused without workers. |
| `SONDER_INFERENCE_PRIVATE_WORKERS` | unset | JSON array of approved private workers, `[{"url": "https://10.77.0.2:8443/sonder-inference", "ca_bundle": "C:/.../ca.pem", "token_env": "SONDER_INFERENCE_NODE1_TOKEN", "max_inflight": 2}]` (see Private workers). Requires `SONDER_ALLOW_REMOTE_INFERENCE=1`. |
| `SONDER_INFERENCE_TIMEOUT_SECONDS` | `300` | Per-call ceiling, never beyond the operation deadline. |
| `SONDER_INFERENCE_HEALTH_TTL_SECONDS` | `5` | How long a health observation is reused. |
| `SONDER_INFERENCE_HEALTH_TIMEOUT_SECONDS` | `5` | Probe budget in seconds, greater than zero and at most 6. Generation retries a timeout/overloaded probe once with twice this budget, capped at 6 seconds and the operation's remaining budget. |
| `SONDER_INFERENCE_HEALTH_STALE_SECONDS` | `120` | Maximum age of healthy evidence usable when a probe times out or reports overloaded (0 disables reuse; maximum 3600). Status/detail reports `busy`; reuse never renews the healthy observation's age. |
| `SONDER_INFERENCE_FALLBACK` | `none` | `none` or `ollama`; anything else fails composition. |
| `SONDER_INFERENCE_THINKING` | `auto` | Forward `think` as `chat_template_kwargs.enable_thinking`: `auto` (when the health document advertises it), `on`, `off`. |
| `SONDER_INFERENCE_SAMPLING_DEFAULTS` | `0` | `1` fills the model family's recommended sampling values for fields the caller left unset. |
| `SONDER_INFERENCE_SAMPLING_TABLE` | unset | JSON list replacing the built-in sampling family table. |
| `SONDER_AGENT_NUM_PREDICT` | `4096` | Decision output cap for `sonder_inference`, including hidden reasoning. Positive integer, capped at 8192; invalid/nonpositive values use 4096. Ollama decisions retain 1200; hosted agent budgets retain their existing separate policy. |
| `SONDER_AUTOPILOT_JSON_NUM_PREDICT` | `4096` | Planner/reviewer JSON output cap for `sonder_inference`, with the same validation and 8192 ceiling. Ollama retains 1800. |
| `SONDER_AGENT_TEMPERATURE` | `0.1` | Agent decision temperature on `sonder_inference`; finite number from 0 to 2. Does not change other providers or ordinary helper calls. |
| `SONDER_AGENT_SAMPLING` | unset | Optional `top_p,top_k,min_p` triple, for example `0.95,20,0`. Applied only to agent decisions on thinking-capable providers (currently `sonder_inference`). Probability values must be in [0,1]; top_k must be a nonnegative integer. |
| `SONDER_AGENT_DECISION_THINKING` | `on` | `on`/`off` request explicit thinking only when this provider's health advertises support; `auto` leaves the serving template's default. The health decision is frozen at construction. Missing support sends no override. |
| `SONDER_AUTOPILOT_JSON_THINK` | `auto` | Planner/reviewer `off`/`on`/`auto`. `auto` requests off only when health advertises thinking; otherwise no override. Ollama is unchanged. |

The agent controls are limited to the decision and planner/reviewer entrypoints;
changing them does not retune chat, learning, summaries or other tier helpers.
Malformed sampling, temperature or thinking settings fail explicitly on those
Inference entrypoints. Ollama and other providers do not read those settings.
Use one configuration per experiment/run; a running decision generator keeps
its initial settings even if the process environment changes.

The request tuning module also exposes an explicit `decision` sampling profile:
thinking temperature 0.6, top_p 0.95, top_k 20, min_p 0;
non-thinking temperature 0.7, top_p 0.8, top_k 20. Agent decisions select that
profile when `SONDER_INFERENCE_SAMPLING_DEFAULTS=1`, filling only unset values.
With no explicit thinking override, the decision row assumes thinking.
It does not replace the historical 0.1 agent temperature;
use the agent environment controls above for E06/E12 sampling experiments.
The bridge carries boolean `think` and explicit `reasoning_budget_tokens` /
`reasoning_budget_message` options to the provider gateway; server-side support
and gateway mapping determine whether those reasoning controls are honored.

Base URL resolution order: `SONDER_INFERENCE_BASE_URL`, then the ready file,
then the default. Invalid values (unknown tier keys, non-numeric timeouts,
`SONDER_ALLOW_REMOTE_INFERENCE` other than `0`/`1`) raise `InvalidInput`.

A busy probe with recent healthy evidence allows generation to proceed. A
definitive failure (credentials, API version, connection refusal or starting/
draining) still refuses the call. A 503 `backend_unavailable` containing
`read timed out` is a `busy_timeout`: retry once at the same endpoint after
`Retry-After` (seconds or HTTP date, capped at 5 seconds), defaulting to 5
seconds for missing/invalid values. Backoff and both sends share the call
budget and honor cancellation; partial streams are never replayed. These
post-send failures never trigger the Ollama fallback.

Calls with a context session send its stable `prompt_cache_key`; worker
sessions also send `X-Sonder-Agent-Id`. Standalone and managed REPL agent
run correlations (`standalone-*`, `repl-work-*`) serve the same purpose when
there is no session. Without a stable identity the cache key is omitted.
`X-Sonder-Run-Id` remains the call's correlation ID. `X-Sonder-Priority` is
`interactive` for HTTP/REPL/MCP, `subagent` for workers and agent runs, and
`background` for system work. Ambient identity is used only for the same
principal. Autopilot/fleet callers must bind a stable run session upstream
to get per-run cache affinity; fresh `tier-helper-*` IDs are not stable keys.

Explicit `reasoning_budget_tokens` (0–1000000) and
`reasoning_budget_message` (at most 16384 characters) pass through alongside
the existing sampling options. `think` keeps the advertised-capability/
operator-override behavior above. Gateway responses extend `ModelResponse`
with optional `timings` and `finish_reason`. Timings accept only bounded
backend measurements: `cache_n`, `prompt_n`, `predicted_n`, `queue_ms`,
`draft_n`, `draft_n_accepted`, preferring `usage.sonder.timings` over legacy
top-level timings, with `usage.prompt_tokens_details.cached_tokens` as a
cache-count fallback. Missing measurements remain absent. The gateway's
thread-local `last_response_meta` and `inference_outcome` activity event
retain this metadata; the public detailed feed carries it in the event
summary, without prompt or response content.

## Consent

- Loopback (`127.0.0.1`, `localhost`, `::1`) needs nothing. Other loopback
  aliases (`127.0.0.2`, ...) are refused with `InvalidInput` at configuration:
  Inference's Host check (contract 2.3) would reject them with 403 anyway.
- Any other host is refused with `Forbidden` before any byte is sent unless
  `SONDER_ALLOW_REMOTE_INFERENCE=1`, the URL is `https://`, an API key is set,
  and (for prompt-bearing calls) the `OperationContext` has `cloud_allowed`.
  The context flag keeps surfaces such as A2A, whose contexts do not allow
  cloud, from sending prompts off-host through an env-only opt-in.
- Health and identity probes apply the URL, TLS and key checks (they carry
  the key) but not the context flag (they carry no prompt).

## Private workers

`SONDER_INFERENCE_PRIVATE_WORKERS` is a separate consent lane for
operator-approved Sonder Inference servers on the owner's private network
(for example a second PC behind a TLS proxy). It is not cloud consent and
grants none: hosted tiers, `cloud_allowed` and remote-Ollama consent are
unchanged by it, and it is unchanged by them.

- It is honoured only with `SONDER_ALLOW_REMOTE_INFERENCE=1`; a list without
  that opt-in is a configuration error, never silently ignored.
- Each worker URL must be `https://`, an IP literal in a private or
  link-local range (never a DNS name, loopback or public address) with an
  explicit port; a path prefix for the proxy is allowed.
- `ca_bundle` (an absolute path to an existing file) is the worker's only
  trust anchor: the system store is not consulted for it, so a publicly
  issued certificate cannot impersonate the worker.
- `token_env` names the variable holding the worker's bearer token (at least
  16 characters). The token is read when used, never stored in the list,
  never sent to another endpoint, and never reported. A worker whose token
  is missing is skipped and shown as such in status. It may not reuse
  `SONDER_INFERENCE_API_KEY`.
- Prompt-bearing calls to an approved worker do not need `cloud_allowed`;
  every other non-loopback endpoint still does. The lane applies to every
  operation that reaches this provider, A2A included; leave the list unset
  where that is not wanted.

Placement is whole-request: each call goes to the endpoint with the lowest
expected cost, `(in-flight + 1) / max_inflight` times its observed
milliseconds per output token, among the primary and the workers that are not
known to be down (fresh health) and serve the requested model; ties rotate.
The primary's `max_inflight` is `SONDER_INFERENCE_MAX_INFLIGHT` (default 1).
The per-token estimate is an EWMA of successful calls with the queueing in
front of each call divided out, and an idle endpoint that has never been
measured is tried once first. A much slower worker therefore receives only
overflow work.

A worker is a candidate only on positive evidence: its health document
(the cached health check, probed when stale) must be ready and must list the
exact requested model id. A worker that has not answered, or does not list
the model, never receives the request, because a 404 there would be final.
The `default` alias never selects a worker, since each server resolves it
to its own model. Tiers whose model only the primary serves therefore stay
on the primary. A call moves to another endpoint
only after `SonderInferenceUnreachable` (provably never executed there);
timeouts, 4xx and 5xx are final, as on a single endpoint. When nothing is
eligible the primary produces the refusal. `provider_status()` adds a
`workers` list (scheme and host, state, models, in-flight; never a path or
token), each response carries `endpoint`, and every placement is logged at
INFO as `sonder-inference placed request on <scheme://host> (primary|private
worker)`.

## Transport

The gateway reuses the `OpenAICompatibleGateway` request path and error
mapping, but injects its own GET and POST transports:

- Environment and OS proxy settings are ignored (`ProxyHandler({})`, as for
  Ollama). A loopback prompt therefore never leaves the host through a proxy,
  and a stopped server shows up as "connection refused" (so the pre-send
  classifier and the fallback work) rather than as a proxy's 502.
- Redirects are never followed. A 3xx is returned (GET) or mapped to
  `DependencyUnavailable` (POST); the `Authorization` header is never re-sent
  to the `Location` host.
- Each exchange (connect, headers and body) has one wall-clock budget: the
  health probe's 2 s, or the send timeout capped by the operation deadline. A
  watchdog shuts the socket down when the budget is spent, so a peer that
  trickles bytes cannot stretch a per-read timeout into minutes.
- Bodies are read with bounds (1 MiB for GET, 16 MiB for a chat completion,
  16 KiB for an error document). A peer that does not speak HTTP (for
  example an SSH banner on the port) is `DependencyUnavailable`, never a
  crash and never "unreachable" for a send.

## Wire format

`POST /v1/chat/completions` with the OpenAI subset: `model`, `messages`
(system, history, user), `stream: false` (`true` with
`stream_options.include_usage` for the first bridged generation of a streamed
HTTP turn; see [request path](../integration/sonder-inference-request-path.md)),
and only the sampling options the caller set (plus family defaults when
`SONDER_INFERENCE_SAMPLING_DEFAULTS=1`). Ollama option names map as `num_predict`→`max_tokens`; `temperature`,
`top_p`, `top_k`, `min_p`, `typical_p`, `seed`, `stop` (string or up to four),
`presence_penalty`, `frequency_penalty`, `repeat_penalty`, `repeat_last_n` and
`num_ctx` pass through by name (the last four as Sonder extensions). `format`,
`tools`, `tool_choice`, `functions` and `response_format` are refused locally
with `InvalidInput`; Inference v1 would reject them anyway. On the legacy
chat path (`_chat_request`), a step that needs one of them (a decoder schema,
native tools, images or tool calls; `provider_bridge.ollama_only_feature`) is
served on local Ollama instead when `SONDER_INFERENCE_FALLBACK=ollama` is set:
the payload's own tier model, the loopback daemon only (never the worker pool,
a `-cloud` model or a remote endpoint, which are refused). The reroute is
logged, announced as `route.changed` (`reason_code=ollama_only_feature`) and
noted in the turn receipt's `degraded` list as `served by ollama (ollama-only
feature: ...)`. Without the declaration the step is refused with a 400 that
names the fix. `think` is
forwarded as `chat_template_kwargs.enable_thinking` when the server advertises
support; otherwise `think=True` is refused and `think=False` dropped.

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
`Forbidden` (401 `unauthorized` names `SONDER_INFERENCE_API_KEY`; 403
`forbidden_host`/`forbidden_origin` names the base URL host instead, which a
key cannot fix); 429 and 503 `overloaded` → `CapacityExceeded`; 3xx, 408,
500, 503 `backend_unavailable`, malformed HTTP and connection resets →
`DependencyUnavailable`; timeouts → `DeadlineExceeded`. Calls are single-attempt, and deadline
and cancellation are checked before the health probe, before the send and
after the response.

Before each send the gateway consults cached health (`GET /v1/sonder/health`,
at most 2 s of wall-clock time, reused for the TTL). Concurrent callers that
find the cache stale share one probe (single flight). The API major version
must be 1, read from the health document's `api_version` (or, for an error
document, its `sonder.api_version`) and, when present, from the response
body's `sonder.api_version` (the shared transport cannot read response
headers). Only a version that is present and differs is a mismatch; it raises
plain `DependencyUnavailable` ("incompatible sonder-inference API"), which
never triggers the fallback. A health answer of 503 `overloaded` is transient:
it is reported as `degraded`, is never cached, and the request fails with
`CapacityExceeded` (no fallback: the server is up). Rejected credentials or
Host on health raise `Forbidden`.

With Inference down and no fallback, the error names the base URL and the
short cause once, then `sonder-infer serve` and `SONDER_INFERENCE_FALLBACK`
(which takes effect after a restart, because bindings are composed at
startup). The health snapshot's `detail` holds only the short cause, for
example `connection refused`.

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
- The fallback call runs under a copy of the `OperationContext` with
  `cloud_allowed=False` and `remote_ollama_allowed=False`. Ollama's own
  consent gate therefore refuses a tier mapped to a hosted (`-cloud`) or
  remote model with `Forbidden`, and the fallback never reaches the cloud,
  whatever the original request allowed.
- The WARNING states the primary's cause once. When the fallback itself
  fails, the raised error keeps the fallback's class and code and appends
  the primary's cause ("fallback to ollama after: ...").
- The fallback target is not a routable binding.
  `ProviderBindings.required_providers` and `bound_providers` both mean
  "routable" and exclude it; `constructed_providers` adds fallback targets and
  is what `build_model_gateway` constructs. Tier dispatch only sees the
  wrapper, and bootstrap's strict local-alias gate
  (`"ollama" in required_providers`) is unchanged by a fallback: a strict
  local-alias request with a fallback-only Ollama fails with `InvalidInput`,
  exactly as without the fallback.
- `build_model_gateway(..., fallback_observer=...)` receives
  `(from_provider, to_provider, reason_code, context)` once per fallback,
  before the fallback send. This is the seam for a `route.changed` event,
  because a refusal from cached health never reaches `dispatch_provider`.
  Bootstrap wires it to `provider_attempts.report_provider_fallback`, so the
  Runtime telemetry stream shows the change even when no send happened.
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
  URLs, or null when unreachable), `fallback`, `fallback_count`,
  `tier_models` (`{tier: model}` for all five tiers: the
  `SONDER_INFERENCE_TIER_MODELS` entry or the default model; from
  configuration, so present even when unreachable). `served_tier_models()`
  returns the same map as `{"sonder_inference": {...}}` without any I/O;
  `PreSendFallbackGateway` forwards it and `ProviderDispatchGateway`
  aggregates it, for the `/v1/models` row `sonder.served_model`.
  `PreSendFallbackGateway` fills in `fallback`/`fallback_count`, and
  `ProviderDispatchGateway.provider_status()` aggregates every provider,
  reporting providers without the method as `{"provider": id, "state": "unknown"}`.

## Doctor, preflight and discovery

- `python -m sonder_runtime doctor` runs `sonder_inference`, whose verdict
  follows `SonderInferenceGateway.readiness()`, i.e. what a request would
  meet: skipped when no binding uses it; ok when ready; warn for the mock
  backend, for a server at its connection limit, or for an outage (refused,
  unresolvable, not ready, missing ready file, malformed answer) that
  `SONDER_INFERENCE_FALLBACK=ollama` covers; fail for an outage without a
  fallback and for anything no fallback can help (invalid `SONDER_INFERENCE_*`
  values, a remote URL without consent, rejected credentials or Host, an API
  version mismatch from health or from the ready file, invalid bindings).
  `sonder_inference_scope` reports the tier-bound agent, autopilot, fleet and
  helper surfaces; explicit Ollama pins, strict `sonder` aliases and durable
  fanout remain outside that binding. `--skip-inference` removes both.
- `serve`/`preflight` add a non-required `sonder_inference` check only; startup
  never blocks on Inference, and a check that fails unexpectedly is reported
  as a failed non-required check rather than raised.
- `environment_probe` lists `sonder-infer` as a specialist tool and
  `toolchain_policy` allows the fixed argument `version` for it.

## Which surfaces use the provider

Provider bindings are honoured by the chat and agent generation consumers:
`ChatService` (`POST /a2a` SendMessage), session summarize/title offload,
interactive agents, workbench turns, autopilot planner/task/review calls,
master/fleet workers, ensembles, web research and audit/helper calls. Each
call resolves its provider from the selected tier. Exact model pins, strict
`sonder` aliases and durable fanout remain explicitly Ollama-bound; image and
schema requests retain the chat refusal for non-Ollama providers (a
`sonder_inference` tier with `SONDER_INFERENCE_FALLBACK=ollama` serves them on
local Ollama instead; see the wire format above), and the
sealed single-send codegen canary refuses a bound non-Ollama provider. The
`/v1/models` listing and escalation rungs may still deduplicate by Ollama model
name; that identity has no meaning for an Inference-bound tier.

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
