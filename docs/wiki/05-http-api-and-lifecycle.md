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
| `GET /v1/models` | key | Route IDs plus exact chat-capable catalog models. |
| `POST /v1/admin/drain` | admin | Begin graceful drain (idempotent). |
| `GET /v1/admin/updates/status` | admin | Durable update state (System page). |
| `POST /v1/memory/replication/batches` | fixed configured peer only | Disabled unless the typed fact-only receiver is enabled; accepts one bounded authenticated replication batch and returns its durable receipt. It is not an operator send, takeover, or failback endpoint. |
| `GET /v1/sonder/status` | admin/owner | Rich host-wide runtime/stats snapshot, including the configured deployment profile and honest capability availability. Ordinary hosted accounts receive only their account and the model catalog. |
| `GET /v1/sonder/feed` | any authorized caller | Owner-scoped live execution feed: the caller's own active and recently completed responses (category/name, state, elapsed, redacted summary, current operation). Never exposes prompts, tool arguments, paths, outputs, reasoning, or another principal's work. |

`/live` may be unauthenticated so an external check never needs the key;
everything else requires the bearer key unless the peer is loopback (the
reverse proxy restricts those paths to loopback upstream).

**Host allowlist (DNS-rebinding defence).** Before any routing, every request
whose `Host` header does not name this listener is refused with
`421 HOST_NOT_ALLOWED` and the connection is closed. Accepted names are
`localhost` and loopback IP literals (a port, when present, must be the bound
port), any IP literal when the listener itself binds a non-loopback address,
and the operator's `[server].allowed_hosts` / `SONDER_ALLOWED_HOSTS` entries
(`name` accepts any port, `name:port` only that port). A request with no
`Host` header (HTTP/1.0 tooling; browsers always send one) is accepted. A
reverse proxy that forwards the public name in `Host` (`proxy_set_header Host
$host`) must list that name in `allowed_hosts`; the reference nginx
configuration forwards the upstream address and needs nothing.

**Client address.** `X-Forwarded-For` is consulted only when
`tls_terminated_by_proxy = true` *and* the socket peer is inside
`trusted_proxy_cidrs`; it is then read right to left and the first hop outside
those networks is the client. Otherwise the socket peer is the client, so a
local process cannot rotate the header to escape the authentication-failure
limiter or spend another address's budget.

`GET /v1/models` always includes the `sonder` runtime route and configured
tier IDs. It also includes exact installed/discovered models that declare a
chat capability; embedding- or vision-only entries are omitted. Cloud models
appear only after the operator enables cloud use, so clients must treat the
response as the live allowlist rather than a static catalog.

The administrator `/v1/sonder/status` projection includes `deployment` with
the configured members, canonical `profile_id` (`single-pc` or `two-pc`), local
control-state scope, and per-capability `available`/`reason` values. A preferred
primary is advisory. In the currently supported profiles, automatic takeover,
failback, explicit promotion, acknowledged state replication, worker-epoch
fencing, and quorum remain explicitly unavailable until their external
authority prerequisites are integrated.

`deployment.recovery_posture` is a read-only summary shared by the API, app,
and REPL. It reports automatic takeover and failback as unavailable and names
the independent-witness, fencing, replication, and ownership-epoch evidence
required before an automatic owner transition can be considered. It neither
contacts a peer nor changes ownership or fence state.

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

The administrator-only `/v1/sonder/status` snapshot also contains
`operational_capabilities` (schema version 1). It is a read-only projection of
already-applied configuration and injected transports: request-level Ollama
pooling, complete-job compute placement, authenticated memory-batch and
content-addressed artifact transfer, plus their bounded limits. When the
memory transport is available, its reason identifies it as fixed-peer,
operator-invoked, project-scoped fact replication: every configured peer must
return a durable receipt before the source cursor advances. That is
all-fixed-peer receipt evidence, not quorum or high availability.

The mobility projection always reports
`automatic_takeover_available: false` and
`automatic_failback_available: false`. It also reports model sharding,
automatic memory/artifact migration, and indefinite-scale providers as
unavailable until those separate ownership and provider systems exist. Reading
the projection never probes a peer, loads a model, enrolls a receiver, changes
runtime state, or invokes `replicate_once()`.

The status snapshot may include a local `memory_replication` service state when
the typed feature is enabled. It contains only the configured peer identities,
bounded cursor, receipt identities/cursors, and stable pending/failure reasons;
it never contains a fact payload, peer origin, checkpoint path, or secret.
There is no public HTTP endpoint that invokes `replicate_once()`. The batch
route above is only the inbound fixed-peer receiver.

The supported chat subset currently includes `model`, `messages`, `stream`,
`session`, `project`, `context_size`, and the consented location fields.
`project` scopes durable facts, which the served route namespaces per
principal. When the value names an existing directory inside the
deployment's configured file roots (`SONDER_FILE_ROOTS` and the roots
file), routed natural work (the workbench agent) runs in that directory;
that scopes the agent to a directory the deployment already exposes and
never widens its reach. A bare name stays a memory namespace, and a
directory outside the roots is ignored for routing (measured in
`../architecture/evidence/MODEL-TRIALS-2026-09-03.md`).

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

## Request-body framing

Steps 2, 3, and 5 above answer before the request body is read, as does
`POST /v1/admin/drain`. A body left on the socket cannot be allowed to become
the next request on a reused connection, so the adapter either takes a small
framed body off the socket and keeps the connection usable, or answers with
`Connection: close`. Clients therefore see `Connection: close` on an oversized
body (413) and on any request whose framing was rejected — a duplicated or
non-numeric `Content-Length`, or a transfer coding, neither of which is
supported. Nothing beyond the accepted request-size limit is ever read.

An optional `Idempotency-Key` header on a POST makes a served action (slash
work controls, permission-mode changes, fanout controls, drain) replay-safe
for that principal and action. A key longer than 512 characters, or a repeated
`Idempotency-Key` header, is rejected with `400 invalid_request` before
dispatch rather than running the action without replay protection.

One key names one request. Reusing a key for a *different* request (another
action, mode, fanout model, or session) is refused and nothing runs. On
`/v1/permission-mode` and `/v1/fanout/<id>/{cancel,resume,synthesize}` a
refusal is never a `200`; it answers with `error.code`:

| Code | Status | Meaning |
|---|---|---|
| `IDEMPOTENCY_KEY_REUSED` | 422 | The key already names a different request. |
| `IDEMPOTENT_ACTION_COMPLETED` | 409 | It completed before this server process; not re-run. |
| `IDEMPOTENT_ACTION_UNCERTAIN` | 409 | An interrupted process left its outcome uncertain; not re-run. |
| `IDEMPOTENCY_CAPACITY_EXHAUSTED` | 429 | Receipt budget full; retry later (`Retry-After`). |
| `IDEMPOTENCY_RECEIPT_UNAVAILABLE` | 503 | Receipt store unavailable; nothing started. |

Chat-routed actions (slash work controls, natural work) keep answering with
the refusal text as the assistant reply.

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
for backend phases and token throughput. Model-call `tier` is only `local` or
`cloud`, and `result` is only `ok` or `error`; exact model names and configured
aliases are never exported. Inference labels are closed sets
(`backend`, `phase`, `direction`, and explicit `cold`/`warm` state); prompts,
responses, model names, and endpoints are never exported. Absent the Prometheus client the
metric calls are cheap no-ops and `/metrics` returns an explanatory comment.
