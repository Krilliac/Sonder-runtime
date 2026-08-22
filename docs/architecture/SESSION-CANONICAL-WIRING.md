# Canonical application session wiring

The composition root now exposes one lazy, cached `session_capture_service`
factory backed by the canonical `SQLiteSessionRepository`. `ChatService` uses
that same factory, so a model turn captured through `Application.chat` and a
caller that reopens the database share one application/session boundary.

`SessionCaptureService.replay(session_id)` is the read-only restart seam. It
verifies the complete SQLite hash chain before reconstructing the model-visible
request and transcript; partial or modified histories fail closed.

The focused evidence is in
`tests/production/test_application_session_wiring.py`: it covers a turn sent
through the application graph, a fresh repository/application replay, and the
privacy boundary that persists provenance digests and labels without source
identifiers, origins, or source bytes. No context, provider, MCP, job, or
root-server migration code is involved.
