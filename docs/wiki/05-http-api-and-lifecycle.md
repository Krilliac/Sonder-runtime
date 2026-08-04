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
work. The reply carries an activity footer of observable actions.

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
`sonder_backup_age_seconds`, and more. Absent the Prometheus client the
metric calls are cheap no-ops and `/metrics` returns an explanatory comment.
