# Observatory live telemetry and the gateway chat bridge

This is the authoritative reference for two Runtime behaviours that the
Sonder ecosystem integration contract v1 (2026-09-26) assigns to this repo:

- Runtime as a live **Observatory producer**: the content-free event
  vocabulary, the stream and discovery routes, correlation with
  Sonder-Inference, and the `GET /v1/sonder/ecosystem` status document.
- The **HTTP chat bridge**: `POST /v1/chat/completions` reaches a non-Ollama
  provider through the `ModelGateway` when a tier is bound to one.

Observatory owns the wire shapes (`protocol/observatory-events.schema.json`,
`protocol/producer-discovery.schema.json` and `docs/TELEMETRY_PROTOCOL.md` in
Sonder-Observatory). The provider itself (`sonder_inference`, its health,
identity and fallback) is described in `docs/architecture/sonder-inference-provider.md`,
which the provider change adds. The security decision is recorded in
[ADR-2026-09-26-observatory-live-telemetry](../adr/ADR-2026-09-26-observatory-live-telemetry.md).

## HTTP chat through the gateway

`server.py`'s chat path builds Ollama-shaped `/api/chat` payloads. Each
escalation rung now binds the provider its tier is bound to:

| Rung tier | Provider |
|---|---|
| `fast`, `general`, `code`, `reasoning`, `vision` | `bindings.tier_providers[tier]` |
| `sonder` (default route) | `bindings.default_generation_provider` |
| `model:*` pins and hosted/cloud tiers | always `ollama` |

When that provider is not `ollama`, the local branch of `_chat_request` hands
the payload to `application/chat/provider_bridge.py`, which converts it to a
`ModelRequest` (system, history and user messages; `temperature`,
`num_predict` and `num_ctx` only; the Ollama model name is dropped) and calls
`model_gateway.generate`. The reply is shaped back into the Ollama form, so
usage counts, receipts and capture are unchanged. The receipt's `model` is
the provider's `ModelResponse.model`.

- The rung binding lives in a ContextVar in `provider_bridge.py`, not in
  `server.py`, so a live reload cannot orphan it.
- The binding is cleared around `model_gateway.generate`, around gateway
  offloads (`_gateway_generate_text`) and around legacy helper calls
  (`_generate_text`). The Ollama gateway re-enters `_chat_request` and takes
  the ordinary Ollama path; it is never intercepted twice.
- The context is the ambient HTTP `OperationContext` (correlation id R, see
  below) with the call's own timeout as its deadline and the host cloud
  policy as its consent. Without an ambient context a local-owner context
  with a fresh correlation id is used.
- Errors: `DependencyUnavailable` ends the turn with HTTP 503 and the
  provider's message; it is never an escalation reason. Decoder schemas
  (`response_format`), `think=True`, reasoning continuation, native tool calls
  and images are refused with HTTP 400. Other domain errors map as follows:
  `Forbidden` 403, `InvalidInput` 400, `CapacityExceeded` 429,
  `DeadlineExceeded` 504, anything else 502.
- Ollama-only side calls are skipped on a bridged rung: the `/api/show`
  context probe, the request-cache model revision (so the request cache is
  not used), model prewarm and the thinking-model probes.
- Memory recall still ranks with the Ollama embedder. When a bridged turn
  gets no query vector it logs a WARNING and names the step in the receipt:
  `sonder_receipt.degraded = ["memory_recall_embeddings"]`.
- With every tier on Ollama nothing above runs.

The REPL, MCP tools, autopilot and fleet paths still call `_make_generate`
directly and therefore stay on Ollama whatever the bindings say. `/v1/models`
and the escalation ladder de-duplicate rungs by their Ollama model name, which
means nothing for a tier served by another provider.

## Live producer

Composition (`bootstrap/app.py`):

```
RuntimeTelemetry (vocabulary, per-turn state, dispatch_provider observer)
  -> RedactingTelemetrySink (application/capabilities/observability.py)
    -> ObservatoryProducer (adapters/observability/observatory_producer.py)
         bounded ring -> GET /v1/observability/events (SSE / NDJSON)
EventSink -> LocalObservabilitySink -> TeeEventSink(OperationsEventSink,
                                                  EventSinkTelemetryBridge)
```

- Instance id `rt-<12 hex>` per composed graph; process session id
  `rts-<same hex>`. `event_id` is `<instance_id>-<sequence>`; sequences are
  contiguous from 0. `mono_ns` is `time.monotonic_ns()` (Linux
  `CLOCK_MONOTONIC`, shared with Sonder-Inference on the same host).
  `wall_time` is RFC 3339 UTC with milliseconds and `Z`. `node_id` is the
  hostname. `producer.role` is `runtime` and `producer.synthetic` is `false`.
- Emission does no I/O and never raises. The ring lock covers numbering and
  one append of an already serialized line.
- A subscriber that falls behind loses its oldest undelivered events. SSE
  announces the loss with a `: dropped <n>` comment; the count is kept as
  `subscriber_dropped_events` in the producer stats. It is not a producer
  drop.
- `telemetry.dropped` reports only events the producer could not sequence (an
  envelope that failed to serialize), with a cumulative `dropped_events`.
- Streams do not take request admission slots. Drain closes them: the
  coordinator flush hook closes every subscription, and each stream also
  stops within one second of the drain starting.

### Routes

| Route | Purpose |
|---|---|
| `GET /.well-known/sonder-telemetry` | Discovery document `sonder.telemetry.producer/1`. Streams `/v1/observability/events` (sse) and `/v1/observability/events?format=ndjson`; links `ecosystem` and `trace`; `vocabularies: {"sonder.runtime.events": 1}`; `stats` with the ring counters. |
| `GET /v1/observability/events` | SSE by default. Format: `?format=ndjson\|sse`, else `Accept: application/x-ndjson`. First write `retry: 2000`; each event `id:` + one-line `data:`; `: keepalive` every 15 s when idle (NDJSON: a blank line). |
| `GET /v1/sonder/ecosystem` | `sonder.runtime.ecosystem/1` (below). |

Resume: `Last-Event-ID` wins over `?last_event_id=`. The same instance with a
retained next sequence replays from there; an older id sends
`: resume-gap <from>-<to>` and the whole window; another instance or no id
replays the whole window; `?since=now` is live only. At most
`SONDER_OBSERVATORY_MAX_SUBSCRIBERS` (default 8) streams run at once; the next
one gets 429 with `Retry-After`.

### Authorization and browser origins

All three routes require administrator authorization exactly as
`/v1/observability/trace` does. In local-open loopback mode that is every
loopback caller; with an API key it is `Authorization: Bearer <key>`.
Discovery reports `auth.required: false` only in local-open mode.

Browser access is controlled by two exact-match allowlists:

- `SONDER_OBSERVATORY_ORIGINS` (`[observability].live_export_origins`) is
  route-scoped: it grants `GET` and `OPTIONS` on the three telemetry routes
  and nothing else. Use this one for Observatory.
- `SONDER_CORS_ORIGINS` is global and also grants every admin route in
  local-open mode. Its preflight answer now includes `Accept`,
  `Cache-Control` and `Last-Event-ID` so streams work for origins already on
  it; do not add an Observatory origin to it just for telemetry.

Operator values for Observatory:

| Observatory | Origin to add to `SONDER_OBSERVATORY_ORIGINS` |
|---|---|
| Dev server (`npm run dev`) | `http://127.0.0.1:5173` and `http://localhost:5173` |
| Preview (`npm run preview`) | `http://127.0.0.1:4173` and `http://localhost:4173` |
| Desktop (Tauri) | `tauri://localhost` and `http://tauri.localhost` |

Then connect Observatory to `http://127.0.0.1:11435` (or open
`/?fixture=0&connect=http://127.0.0.1:11435`). The serve banner prints the
discovery URL.

Remote telemetry needs https (a TLS-terminating proxy) and the admin bearer
key. A read-only, short-lived telemetry capability is not specified; until it
is, a remote Observatory needs the admin key and the desktop shell only works
without a token in local-open mode.

## Event vocabulary v1

`sonder.runtime.events` major 1. Every event is content-free:
`text_capture` is `none` and `sampling.level` is `metrics`. Prompts,
responses, summaries, headers, URLs and provider payloads are never exported.
Model labels are at most 96 identifier characters; free text in a model field
is replaced with `[unsafe-label]`.

| Event | Attributes | When |
|---|---|---|
| `session.started` | `role: "runtime"`, `version`, `text_capture: "none"`, `provider_bindings {default_generation_provider, tier_providers, embedding_provider, fallbacks}` | graph composition |
| `session.ended` | `emitted_events`, `dropped_events` | best effort when the graph closes |
| `request.started` | `surface: "http.chat_completions" \| "a2a"`, `kind: "chat"`, `stream`, `requested_model`, `workload` | turn start, after request validation |
| `route.selected` | `provider: "ollama" \| "openai_compatible" \| "sonder_inference"`, `operation: "chat" \| "generate"`, `model` (the provider-reported model when the reply names one), `attempt` (1-based in the turn), `status: "ok" \| "error"`, `error_code?` | once per provider send inside a turn, from the `dispatch_provider` observer |
| `route.changed` | `from_provider`, `to_provider`, `reason_code`, `attempt` | attempt k's provider differs from attempt k-1's, or a fallback wrapper reports a pre-send fallback |
| `request.completed` \| `request.failed` \| `request.cancelled` | `outcome`, `total_ms`, `http_status`, `provider`, `model`, `attempts`, `prompt_tokens?`, `completion_tokens?`, `error_code?` | exactly once per started turn |
| `telemetry.dropped` | `dropped_events` (cumulative), `emitted_events`, `queue_capacity`, `final` | producer drops |

Provider ids come from the `dispatch_provider` labels, which stay unchanged
as capture evidence: `ollama` to `ollama`, `openai-compatible` to
`openai_compatible`, `sonder-inference` to `sonder_inference`.

Provider sends outside a turn (background distillation, REPL, MCP) are not
exported in v1. A request rejected before `request.started` (origin,
framing, authentication, validation) has no terminal event either.

Bridged EventSink events: only `model.escalation.decided`,
`model.escalation.outcome` and `agent.delegation.accepted` cross into the
stream. `summary` is dropped; `detail` is cleaned with the
`LocalObservabilitySink` field rules (`sanitize_event_detail`);
`correlation_id` becomes `request_id` and `operation_id` becomes `run_id`.
Authentication failures and permission receipts never leave the host.

## Turn correlation

The turn id R is:

- `POST /v1/chat/completions`: the HTTP correlation id, returned as
  `X-Sonder-Correlation-Id`.
- `POST /a2a` `SendMessage`: the `messageId` when it matches
  `[A-Za-z0-9._:-]{1,128}`, else the HTTP correlation id
  (`runtime_telemetry.turn_id`). The same R is the provider-facing
  `context.correlation_id`.

`serve.py` binds the request's `OperationContext` as the ambient context for
the turn (`application/context.py`: `bind_operation_context`,
`current_operation_context`). Runtime turn events carry `request_id = R` and
`run_id = R`; `session_id` is the chat session id when one was supplied, else
the process `rts-` id. Gateway offloads during the turn reuse R as their
correlation id. The `sonder_inference` provider sends R as
`X-Sonder-Parent-Request-Id` and `X-Sonder-Run-Id`, so Sonder-Inference events
for the request carry `run_id = R` and `attributes.parent_request_id = R`.

## Ecosystem status

`GET /v1/sonder/ecosystem` (administrator) returns:

```json
{"schema": "sonder.runtime.ecosystem/1", "generated_at": "...",
 "runtime": {"version", "instance_id", "node_id", "base_url"},
 "providers": {"default_generation_provider", "tier_providers": {"fast", "general", "code", "reasoning", "vision"},
               "embedding_provider", "fallbacks": {}, "status": {"<provider>": {...}}},
 "observatory": {"export_enabled", "runtime_stream": {"discovery_url", "sse_url", "ndjson_url"} | null,
                 "stats": {"subscribers", "emitted_events", "dropped_events", "retained_events", "buffer_capacity"},
                 "cors_origins": [], "connect_urls": [], "warnings": []}}
```

- `providers.status` comes from the model gateway's `provider_status()`; a
  gateway or provider without it reports `{"provider": id, "state": "unknown"}`.
- URLs use the loopback listener address (`127.0.0.1` for a `0.0.0.0` bind).
- `connect_urls` lists the Runtime base URL (when export is on) and the base
  URL of every provider whose status names telemetry URLs.
- `cors_origins` lists every origin that may read the telemetry routes.
- Warnings cover `embedding_provider = sonder_inference`, a missing
  Observatory origin, disabled export, synthetic providers, and generation
  bindings that REPL, MCP, autopilot and fleet do not honour.
- 404 when export is disabled and the gateway has no `provider_status`.

## Configuration

| Variable | `[observability]` key | Default |
|---|---|---|
| `SONDER_OBSERVATORY_EXPORT` | `live_export` | `1`; `0` makes the three routes 404 |
| `SONDER_OBSERVATORY_BUFFER` | `live_export_buffer` | `4096`, clamped to 256..65536 |
| `SONDER_OBSERVATORY_MAX_SUBSCRIBERS` | `live_export_max_subscribers` | `8` (1..64) |
| `SONDER_OBSERVATORY_ORIGINS` | `live_export_origins` | empty |

## Open questions

These are cross-repo shapes the contract does not pin; they are recorded here
rather than invented.

- The provider fallback wrapper reports a pre-send fallback through
  `provider_attempts.report_provider_fallback(from, to, reason_code)`, which
  forwards to the telemetry observer's `provider_fallback`. The provider change
  must call it for `route.changed` to appear when no send happened.
- `subscriber_dropped_events` is exposed in the discovery `stats` block; the
  Observatory discovery schema pins no field for it (additive keys allowed).
- A read-only telemetry capability for remote or token-bearing Observatory
  connections is unspecified; today the admin key is required.

## Behavior status

| Behavior | Status | Boundary |
|---|---|---|
| HTTP chat and A2A through a non-Ollama provider | Experimental | Local model steps only; REPL, MCP, autopilot and fleet stay on Ollama. |
| Observatory live producer (`/v1/observability/events`, discovery) | Experimental | Admin-gated, content-free, loopback by default; same-host clock merge only. |
| `GET /v1/sonder/ecosystem` | Experimental | Provider rows are `unknown` unless the gateway reports `provider_status()`. |
| Read-only telemetry capability | Unsupported | Remote telemetry needs the admin bearer key. |
