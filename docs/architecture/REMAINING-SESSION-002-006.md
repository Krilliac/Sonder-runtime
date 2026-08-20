# SESSION-002/004/005 and SEAM-006 — integrated model-visible capture

`SessionCaptureService` is the session-domain write/read composition seam. It
uses the existing append-only `SessionRepository`, `crash_safe_replay`, and
bounded `SessionQueryEngine` rather than introducing a second persistence or
replay implementation.

## Contract

`capture_turn` validates a request identity, turn identity, bounded tool
facts, UI facts, and JSON-safe model-visible values. It appends the exact
request snapshot first, followed by optional user, tool-call/tool-result, and
model-response events. No update or delete operation exists. A process crash
between appends leaves a durable prefix; replay remains fail-closed when the
caller cannot prove a complete tail.

After the append sequence, the service runs `crash_safe_replay` and
`SessionQueryEngine.export_events` over the same repository. The returned
`CapturedTurn` therefore carries the committed events, reconstructed request,
transcript, operational projection, integrity report, and deterministic JSONL
export as one evidence-bearing result. UI facts and the request tool manifest
are retained in the model-visible snapshot; consequential tool results are
retained in the replay transcript and projection counts.

The request snapshot includes a canonical SHA-256 digest. When a request has
prompt provenance, only the existing redacted provenance metadata crosses the
durable event boundary. Raw source identity and secrets are not added to the
session event payload.

## Evidence

- `tests/test_remaining_session_002_006.py`
- existing `tests/test_remaining_session_durable_replay.py`
- existing `tests/test_remaining_session_query_export.py`
- focused tests, architecture and requirement-evidence gates, `compileall`,
  and `git diff --check`

The master checklist and conservative audit are intentionally unchanged.
