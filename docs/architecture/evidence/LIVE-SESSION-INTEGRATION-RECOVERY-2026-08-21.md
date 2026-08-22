# Live session integration and recovery slice — 2026-08-21

## Scope

The live legacy server turn boundary now forwards completed model turns into
the canonical typed `SessionCaptureService` through the application
composition graph. Capture occurs after model success and response policy/code
repair, but before interaction/footer decoration. Model failures, web-policy
refusals, and synthetic error text therefore do not become durable model
history. A durable capture failure is propagated instead of reporting a
successful turn whose recovery stream is unavailable.

The existing application graph continues to provide the lazy SQLite session
repository, replay/query/export facade, and compaction service. HTTP session
routes remain read-only and administrator-gated, and the production test now
proves that the HTTP adapter receives the application-owned facade over the
same repository used by capture and replay. Restart recovery uses the
repository hash chain and bounded replay path; no interrupted model work is
automatically rerun.

## Implemented paths

- `server.py`
- `sonder_runtime/bootstrap/app.py`
- `sonder_runtime/application/session/capture.py`
- `sonder_runtime/application/session/durable_replay.py`
- `sonder_runtime/application/session/http_facade.py`
- `sonder_runtime/interfaces/http/facades/session.py`
- `sonder_runtime/interfaces/http/serve.py`
- `tests/test_live_session_recovery_integration.py`
- `tests/production/test_application_session_wiring.py`

## Verification

Focused command:

```text
python -m pytest -q --basetemp .pytest-live-session-recovery \
  tests/test_live_session_recovery_integration.py \
  tests/test_chat_session_capture.py \
  tests/production/test_application_session_wiring.py \
  tests/test_http_session_integration.py
```

The focused suite proves live-boundary capture, database reopen/replay,
request reconstruction, integrity validation, HTTP read/export/replay routing,
application-root HTTP-facade injection over the canonical repository, and
fail-closed propagation when the durable capture dependency is unavailable.

Additional composition command:

```text
python -m pytest -q --basetemp <fresh-temp> \
  tests/production/test_application_session_wiring.py \
  tests/production/test_session_continuity_wiring.py \
  tests/test_http_session_integration.py tests/test_http_session_facade.py
```

Result: **15 passed**.

## Limitations

This is a bounded integration slice. Legacy memory/session projections remain
for compatibility and are not replaced by the typed event stream. Checkpoint
and retention/privacy services remain explicit application/adapter contracts;
they are not silently exposed as new mutating HTTP operations. Streaming,
web-routed synthetic responses, and non-chat job/provider lanes remain outside
this change. The requirements ledger remains `implemented_unverified` pending
the independent master checklist and broader deployment evidence.
