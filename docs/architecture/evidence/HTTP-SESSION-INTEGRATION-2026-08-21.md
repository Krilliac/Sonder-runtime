# Typed HTTP session integration — 2026-08-21

## Scope

The existing `HttpSessionFacade` is now composed into the stdlib HTTP handler
through a narrow, root-free route seam. `serve.main()` constructs it from the
canonical application's `session_repository()` factory; tests may inject a
facade through `configure_session_facade()`.

## Production routes

- `GET /v1/sessions/{session_id}/events` — bounded, redacted event page.
- `GET /v1/sessions/{session_id}/export` — bounded, redacted export.
- `GET /v1/sessions/{session_id}/replay` — integrity-checked replay projection.

The handler performs existing HTTP authentication and restricts these raw
durable-session identifiers to the local owner/administrator boundary. Query
shape and facade result mapping live in
`sonder_runtime/interfaces/http/facades/session.py`; no route imports the
legacy root runtime or changes existing chat/job routes.

## Evidence

`tests/test_http_session_integration.py` passes 3 tests covering all three
routes, bounded query conversion, invalid route handling, composition
injection, and the absence of a direct `server` import. Existing
`tests/test_http_session_facade.py` continues to cover redaction, bounds, and
integrity-safe failures at the application boundary.

## Boundary remaining

Hosted per-account session identifiers are intentionally not exposed by this
smallest integration slice. A future account-scoped projection must define a
durable ownership/index contract before relaxing the administrator gate.
