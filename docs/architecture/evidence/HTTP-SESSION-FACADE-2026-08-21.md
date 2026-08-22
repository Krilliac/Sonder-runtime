# HTTP session facade evidence — 2026-08-21

This slice adds `sonder_runtime.application.session.http_facade.HttpSessionFacade`,
a transport-neutral result adapter over the existing `SessionRepository` port.
It does not add routing, authentication, persistence, jobs, compaction, context,
OpenAI, MCP, or root migration behavior.

The facade has three read-only operations:

- `read` delegates to `SessionQueryEngine.query_events`, preserving bounded
  pagination and recursive payload redaction.
- `export` delegates to `SessionQueryEngine.export_events`, preserving the
  replay-compatible event envelope, integrity metadata, and event bound.
- `replay` delegates to `crash_safe_replay` before returning only replay safety,
  sequence, request-presence/digest metadata, and the redacted transcript.

Malformed requests return a generic `400` error body. Integrity or replay
failures return a generic `409` body; raw event payloads and exception details
are never included in error responses.

Focused verification:

```text
python -m pytest tests/test_http_session_facade.py -q
```

The tests cover bounded reads, redacted exports and replay, invalid query
handling, and fail-closed integrity errors.
