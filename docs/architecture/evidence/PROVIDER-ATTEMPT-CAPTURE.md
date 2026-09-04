# Bounded provider attempt capture

2026-09-04. `ChatService.complete` binds its P1a admission to a context-local
provider scope. `server._post` records each invocation of its selected-worker
send callback for `/api/chat` and `/api/generate`; OpenAI-compatible `_post`
records the final transport payload. Payload transformation, thinking retries,
context compaction, and pool failover remain owned by their existing policies.

`provider.requested` stores the effective JSON body and a fresh attempt identity
correlated to the admitted request and turn. `provider.responded` stores the
returned JSON; it does not assert semantic validity, verifier acceptance, or
final-answer selection. `provider.failed` stores an allowlisted domain code,
never raw exception text. A failed call may still have reached the provider.
An admission with no terminal event remains unknown and grants no replay right.

These events do not project as transcript messages or replace the logical
request snapshot. Existing response ownership remains unchanged. JSON capture
uses the existing two-million-byte bound. Headers and endpoint URLs never enter
the capture API; prompt/response bodies remain private session data.

Capture write errors raise `ProviderCaptureFailure`, an `IntegrityFailure`.
They poison the current scope to prevent swallowed errors from causing another
dispatch or a successful scope exit. Abrupt process termination leaves admission
evidence unresolved. A context scope must be bound in the execution thread;
it does not implicitly propagate into executor workers.

This is not repository-wide coverage. Unscoped legacy calls, direct facade
gateway calls without an owner, separate embedding transports, and injected
custom transports that bypass these dispatch seams remain outside this slice.
Scopes never manufacture sessions or requests at a low-level transport call.

Evidence: `tests/test_provider_attempt_capture.py` exercises durable admission,
response copy, failure/termination, failed writes, scope fencing, two provider
integrations, thinking-option retry, pool failover, transcript exclusion, and
payload bounds. Existing typed-capture, OpenAI, local-retry, and worker-pool
regressions are also run. These tests use offline transports, not live models.
