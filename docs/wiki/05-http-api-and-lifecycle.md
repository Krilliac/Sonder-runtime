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
| `GET /v1/tools/inventory` | admin | Redacted host developer-tool inventory; optional `category` and `name` filters. See [Host tool inventory](../host-tool-inventory.md). |
| `POST /v1/tools/inventory/refresh` | admin | Force host tool rediscovery (`{}` or `{"full": true}`); returns the same view. |
| `POST /v1/tools/test-run`, `GET /v1/tools/test-run/<id>` | admin (own runs only) | Structured test run through the typed `test_run` / `test_run_result` tools, graded unattended by the permission modes (`test_run` is execution). `202` with the status while running, `200` with the report. Cancel with `POST /v1/jobs/<id>/cancel`. See [Structured test runs](../structured-test-runs.md#over-http). |
| `POST /v1/tools/output-digest` | admin | Typed `output_digest` of exactly one of an owned test-run `job_id` or a guarded `path`. |
| `GET /v1/admin/updates/status` | admin | Durable update state (System page). |
| `POST /v1/memory/replication/batches` | fixed configured peer only | Disabled unless the typed fact-only receiver is enabled; accepts one bounded authenticated replication batch and returns its durable receipt. It is not an operator send, takeover, or failback endpoint. |
| `GET /v1/sonder/status` | admin/owner | Rich host-wide runtime/stats snapshot, including the configured deployment profile and honest capability availability. Ordinary hosted accounts receive only their account and the model catalog. |
| `GET /v1/work-runs`, `GET /v1/work-runs/<id>` | developer/admin (own runs only) | Routed-work runs started by this principal: status (`running`, `returned`, `unknown`, `refused`, `cancelled`, `budget_exceeded`, `interrupted`, `failed`) and, for one run, its persisted answer. |
| `POST /v1/work-runs/<id>/cancel` | developer/admin (own runs only) | Cancel a routed-work run: its effect fence stops holding, so every further file change, host program, or destructive tool is refused. |
| `GET /v1/approvals` | developer/admin | Calls refused unattended that can be approved once (`pending`: call id, tool, redacted preview, count) and open approvals (`approvals`); `?limit=1..200`, `?include_spent=1` adds spent, revoked and expired ones. |
| `POST /v1/approvals/<call_id>` | developer/admin | Approve exactly one refused, still-pending call once (body `{}` or `{"ttl_seconds": 60..86400}`, default 900; optional `tool`/`digest` must match). `201` with the approval. See [One-shot approvals over HTTP](#one-shot-approvals-over-http). |
| `POST /v1/approvals/revoke/<nonce>` | developer/admin | Withdraw one open approval (body `{}`). |
| `GET /v1/sessions` | admin | Durable sessions, newest activity first: `id`, redacted `title` (first user message, one line, at most 80 characters), `turns`, `events`, `created`, `updated`; `?limit=1..100` (default 20) and `?after=<next_cursor>`. Ids are storage ids, the same ones `/v1/sessions/<id>/{events,export,replay}` take. |
| `POST /v1/sonder/register` | bootstrap secret, or admin when additional registration is enabled | Create an account; `201 {"ok": true, "account": {...}, "message": "Account <u> created (role <r>)."}`. |
| `GET /v1/sonder/feed` | any authorized caller | Owner-scoped live execution feed: the caller's own active and recently completed responses (category/name, state, elapsed, redacted summary, current operation). Never exposes prompts, tool arguments, paths, outputs, reasoning, or another principal's work. |

`/live` may be unauthenticated so an external check never needs the key;
everything else requires the bearer key unless the peer is loopback (the
reverse proxy restricts those paths to loopback upstream).

**Host allowlist (DNS-rebinding defence).** Before any routing, the `Host`
header is checked. DNS rebinding needs a hostname the attacker controls and a
listener that answers without credentials, so:

- any IP literal is accepted on any port (`10.0.2.2` from the Android
  emulator, LAN and Tailscale addresses, `127.0.0.1:<forwarded port>`), except
  the unspecified `0.0.0.0` / `[::]`;
- `localhost`, `*.localhost` and this machine's own names are accepted on any
  port. The machine names are the host name, its FQDN and `<hostname>.local`,
  computed once at startup from `gethostname`/`getfqdn` (the FQDN lookup is
  bounded to one second and dropped if it does not finish);
- `[server].allowed_hosts` / `SONDER_ALLOWED_HOSTS` entries are accepted
  (`name` on any port, `name:port` on that port only);
- any other well-formed name is accepted when the listener requires
  credentials (`api-key`, `account`, `both` or `either`), because a rebinding
  page holds none; in the unauthenticated `local-open` mode it is refused.

A refusal is `421` with `error.code = "HOST_NOT_ALLOWED"` and an
`error.remedy` naming `[server].allowed_hosts` / `SONDER_ALLOWED_HOSTS`, and
the connection is closed. The server logs a WARNING with the refused name, at
most once a minute per name. Malformed or repeated `Host` headers are always
refused; a request with no `Host` header (HTTP/1.0 tooling; browsers always
send one) is accepted. A name accepted only because credentials are required
does not earn the loopback-peer conveniences: through it, `/ready`, `/health`,
`/version` and `/metrics` need the key even from a local browser, and the
local log page is not served.

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
work. A client that owns its transcript and must never have server history
injected (for example after it cancelled a thread's first turn) sends
`"history": "client"`; the default is `"auto"`, and any other value is a
`400`. `choices[0].message.content` contains only the answer; bounded
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
`session`, `project`, `history`, `context_size`, and the consented location
fields.
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

For an ordinary model turn with `stream: true`, the response commits to SSE
as soon as generation starts: the `200` headers and an SSE comment
(`: keep-alive`) are sent immediately and repeated every
`[server].stream_heartbeat_seconds` (default 15, `SONDER_STREAM_HEARTBEAT_SECONDS`)
until the answer is ready, so client and proxy idle timeouts no longer expire
during a slow CPU generation. The answer itself still arrives as one content
chunk: the generation pipeline (retrieval, critic and retry passes,
escalation) produces it only when the turn completes, so it is not
token-streamed. `X-Sonder-Elapsed-Ms` on such a stream is the time to the
headers; the final chunk's `sonder_elapsed_ms` is the full duration. Because
the status is already `200`, a model or capture failure after that point is
delivered as one terminal SSE event with `"object": "error"` and an
`error.code` (`MODEL_CALL_<status>`, `SESSION_CAPTURE_UNAVAILABLE`,
`INTERNAL_ERROR`), followed by `[DONE]`. Slash, web, work, structured, and
multi-sample (Spanda) turns keep the previous framing, and non-streaming
responses are unchanged.

`context_size` must be absent, `null`, `""`, or a positive token count
(`8192`, `"32k"`, `"1m"`); anything else is `400 invalid_request` instead of
silently selecting the default window. Surrounding whitespace in `model` is
ignored and not echoed back. On the default `sonder` route, a message that is
only one unknown `/word` is answered with a short "no command with that name"
reply instead of a model call; a sentence that merely starts with `/` still
reaches the model.

## Routed work runs

A chat turn that the host routes to an execution lane (workbench, fleet, or
autopilot) runs as a **work run** with id `wr-…`:

- The request waits at most `[server].work_wait_seconds` (default 240,
  `SONDER_HTTP_WORK_WAIT_SECONDS`). A run that finishes in time answers
  inline; otherwise the reply names the run id and `sonder_receipt.chat_work`
  carries `status: "running"`, `work_run_id`, and the routes as data
  (`get_url`, `cancel_url`); the reply text itself names no HTTP routes. The
  answer is persisted and returned by `GET /v1/work-runs/<id>` (bounded to
  256 KiB, retained 7 days).
- `[server].work_budget_seconds` (default 1800,
  `SONDER_HTTP_WORK_BUDGET_SECONDS`) is a wall-clock budget. After it, or
  after `POST /v1/work-runs/<id>/cancel`, the run's effect fence no longer
  holds: the permission gate refuses every further file change, host program,
  or destructive tool, and the run ends as `budget_exceeded` or `cancelled`.
  A model step already in flight cannot be preempted from the HTTP layer; the
  lane's remaining steps run to their step bound without effects. The fence
  covers effects on the run's own thread (the workbench lane). Fleet workers
  and autopilot runs execute on their own threads under their own fences, so
  cancelling the work run does not stop them; use their cancel surfaces
  (`/master_cancel`, `/autopilot cancel`).
- A shutdown drain also fences every work run: once the runtime is draining,
  further effects are refused and the run ends as `interrupted`. A run that
  outlived its request still counts as an in-flight mutation, so the drain
  waits its bounded deadline for the current effect to finish.
- At most `[server].work_max_running` (default 2,
  `SONDER_HTTP_WORK_MAX_RUNNING`) runs execute at once; another routed turn
  is refused with `429 WORK_CAPACITY_EXHAUSTED` and `Retry-After`.
- A run left `running` by a stopped process is reported `interrupted` after
  restart.

## One-shot approvals over HTTP

When the permission gate refuses a file change, host program or destructive
tool because nobody is present to answer the mode's ask, and the call carried
arguments, the refusal names the call (`/approve <call id>` in the text) and
notes it as pending in the approval ledger. The chat response then also
carries it as data, in `sonder_receipt.refusal`:

```json
{ "kind": "refused", "tool": "file_write", "call_id": "3f9a12c0d4e5b6a7",
  "risk": "mutation", "mode": "manual", "mode_label": "manual",
  "reason": "file_write changes files and nobody is here to answer ...",
  "remedies": [
    {"kind": "approve_once", "call_id": "3f9a12c0d4e5b6a7", "method": "POST",
     "path": "/v1/approvals/3f9a12c0d4e5b6a7", "console": "/approve 3f9a12c0d4e5b6a7"},
    {"kind": "switch_mode", "modes": ["acceptEdits", "auto"]},
    {"kind": "allow_rule", "console": "/permissions"},
    {"kind": "console", "detail": "run it from the console and answer the prompt"} ] }
```

The text is unchanged. `/write`, `/append` and `/edit` typed in chat name
their call the same way the catalogued `/file_write path=… content=… mode=…`
spelling does, so either spelling is approved by the same id. When a turn
refuses several calls, the last is described and `refusals_in_turn` counts
them.

`POST /v1/approvals/<call_id>` is the attended answer to that ask. The
security decision: an authenticated developer or administrator POST is an
attended approval surface, the same precedent as `POST /v1/permission-mode`.
The person holding the credential approves one exact call:

- only a call that was refused and is still pending can be approved; there is
  no approve-in-advance over HTTP (the console's `/approve call` keeps that);
- the path takes the 16-character call id or the full 64-character digest,
  never a shorter prefix, and a body `digest` or `tool` that differs from the
  pending call is refused with `409 CALL_DIGEST_MISMATCH`. Changing any
  argument makes a new call that needs its own approval;
- a call has at most one open approval: a second POST answers
  `409 APPROVAL_ALREADY_OPEN` with the existing one. An `Idempotency-Key`
  retry replays the first response, with the refusal codes listed below;
- the ledger spends the approval atomically on the next unchanged call from
  any surface and it expires after `ttl_seconds`; the mode is not changed;
- the approver is recorded as `developer:<username>`, `admin-key` (the
  deployment API key) or `local-open`, with surface `http`, and the action is
  audited on the direct-tool path as `permission_approve`.

Errors: `401` without credentials, `403 FORBIDDEN` for an ordinary account,
`400 INVALID_CALL_ID` / `INVALID_TTL` / `INVALID_REQUEST`,
`404 CALL_NOT_PENDING` (already approved and run, aged out, or never refused),
`404 APPROVAL_NOT_OPEN` on revoke, and `503 APPROVALS_UNAVAILABLE`.

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
8. Execute. Model turns are bounded by the provider timeout; routed work runs
   by the wall-clock budget and cancel surface above, which fence effects but
   cannot preempt a model call already in flight.
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
work controls, permission-mode changes, approvals, fanout controls, drain) replay-safe
for that principal and action. A key longer than 512 characters, or a repeated
`Idempotency-Key` header, is rejected with `400 invalid_request` before
dispatch rather than running the action without replay protection.

One key names one request. Reusing a key for a *different* request (another
action, mode, fanout model, or session) is refused and nothing runs. On
`/v1/permission-mode`, `/v1/approvals/…` and
`/v1/fanout/<id>/{cancel,resume,synthesize}` a
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
