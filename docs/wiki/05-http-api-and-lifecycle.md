# HTTP API & Lifecycle

The HTTP adapter (`sonder_serve.py`) is OpenAI-compatible for chat, plus a
production lifecycle and admission layer (`sonder_lifecycle.py`).

## Endpoints

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /live` | none | Liveness — returns only `{"status":"alive"}`. |
| `GET /ready` | loopback or key | Readiness — 200 only when serving and required deps healthy. |
| `GET /health` | loopback or key | Process/dependency state, build, schemas, draining. |
| `GET /version` | loopback or key | Build version + commit. |
| `GET /metrics` | loopback or key | Prometheus exposition (or a disabled comment). |
| `POST /v1/chat/completions` | key | OpenAI-compatible chat. |
| `GET /v1/models` | key | Advertised tiers. |
| `POST /v1/admin/drain` | admin | Begin graceful drain (idempotent). |
| `GET /v1/admin/updates/status` | admin | Durable update state (System page). |
| `GET /v1/sonder/status` | key | Rich runtime/stats snapshot. |

`/live` may be unauthenticated so an external check never needs the key;
everything else requires the bearer key unless the peer is loopback (the
reverse proxy restricts those paths to loopback upstream).

## Chat request

```json
POST /v1/chat/completions
{ "model": "sonder",
  "messages": [{"role":"user","content":"..."}],
  "session": "optional-session-id",
  "project": "optional-project-scope" }
```

A full chat UI owns conversation state (resends the transcript). A thin
client that names a `session` but sends only the current message gets
server-side history rebuilt from the stored session — so both contracts
work. `choices[0].message.content` contains only the answer; bounded
observable execution metadata is returned separately as `sonder_activity`.

The supported chat subset currently includes `model`, `messages`, `stream`,
`session`, `project`, `context_size`, and the consented location fields.

Non-streaming responses populate standard OpenAI `usage` from the current
request's observed model counters. For an SSE response, request an additional
terminal usage chunk with:

```json
{ "stream": true, "stream_options": { "include_usage": true } }
```

That final chunk has an empty `choices` array and a `usage` object. It appears
immediately before `[DONE]`; ordinary streams remain unchanged.

`response_format` is available only for an isolated direct-model turn:

```json
{ "response_format": { "type": "json_object" } }
```

or a deliberately small strict-schema contract:

```json
{ "response_format": {
  "type": "json_schema",
  "json_schema": {
    "name": "result",
    "strict": true,
    "schema": {
      "type": "object",
      "required": ["ok"],
      "properties": {"ok": {"type": "boolean"}},
      "additionalProperties": false
    }
  }
} }
```

The runtime sends that schema as Ollama's decoder-side `format`, then parses
and fully post-validates the direct model text before returning it. It supports
only typed object/array/scalar nodes plus `enum`, `const`, properties/items,
additional-properties, length/count, uniqueness, and numeric-bound keywords;
references, combinators, patterns, annotations, and untyped nodes are rejected
with `400 invalid_request`. `json_schema` must contain exactly `name`,
`schema`, and `strict: true`.

Structured turns do not use slash commands, natural model selectors, feedback,
web, execution, tool, code-repair, activity-footer, or history-learning paths;
an apparent control route is rejected with `400 invalid_request`. This keeps
the returned assistant content exactly the validated model output. Normal model
selection, cloud opt-in/privacy checks, and `stream: true` SSE framing remain
unchanged; the whole validated JSON document is emitted in the normal final
assistant SSE chunk (not token-streamed).

## Process & dependency state

`sonder_service_state.py` tracks one process state and per-dependency
states, with validated transitions:

```
STARTING → MIGRATING → READY ⇄ DEGRADED → DRAINING → STOPPING
                         └────────────────→ FAILED
```

- **READY** requires valid config, writable state, compatible schemas,
  readable policy, and Ollama reachable when the profile needs inference.
- **DEGRADED** = an optional dependency lost; still serves.
- Losing a **required** dependency (Ollama) makes `/ready` 503 with the
  dependency named, while `/live` stays 200 — no false success. A
  background probe recovers readiness automatically (~15s).

## Admission (per chat request)

1. Correlation ID assigned.
2. Peer / trusted-proxy validation.
3. Auth-failure token-bucket limiter (per peer).
4. Authentication (constant-time key compare; rotation overlap honored).
5. Header/body-size limits.
6. Bounded concurrency slot; queue-depth cap; admission deadline.
7. Parse/validate; resolve privilege.
8. Execute under a deadline + cancellation token.
9. Structured completion + metrics.

Rejections use one standard envelope:

```json
{ "error": { "code": "CAPACITY_EXHAUSTED", "message": "...",
             "correlation_id": "req_...", "retryable": true } }
```

Codes: `CAPACITY_EXHAUSTED` (429, queue full), `ADMISSION_TIMEOUT` (504),
`MAINTENANCE_MODE` / `DRAINING` (503), `AUTH_RATE_LIMITED` (429),
`UNAUTHENTICATED` (401). OpenAI-compatible error shapes are preserved
alongside.

## Graceful drain

On `SIGTERM`/`SIGINT` or `POST /v1/admin/drain`: state → DRAINING, reject
new mutating work, cancel non-durable foreground requests, let durable
task steps reach a checkpoint, mark unfinished ownership interrupted at
the deadline, flush logs/events, close databases, stop. The drain
deadline is below the service manager's kill timeout. See
[start-stop-drain](../runbooks/start-stop-drain.md).

## Metrics

Bounded Prometheus metrics (no high-cardinality labels): `sonder_build_info`,
`sonder_process_state`, `sonder_requests_total{route,result}`,
`sonder_request_duration_seconds`, `sonder_active_requests`,
`sonder_model_calls_total{tier,result}`, `sonder_auth_failures_total{reason}`,
`sonder_backup_age_seconds`, plus content-free measured-inference histograms
for backend phases and token throughput. Inference labels are closed sets
(`backend`, `phase`, `direction`, and explicit `cold`/`warm` state); prompts,
responses, model names, and endpoints are never exported. Absent the Prometheus client the
metric calls are cheap no-ops and `/metrics` returns an explanatory comment.
