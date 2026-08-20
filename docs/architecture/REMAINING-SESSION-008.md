# SESSION-008 — bounded session query and export

Status: implemented as an application service; master checklist and audit intentionally unchanged.

## Contract

`sonder_runtime.application.session.SessionQueryEngine` is a read-only facade
over the `SessionRepository` port. It provides:

- bounded, deterministic event search by session, event type, and payload text;
- opaque, query-bound sequence cursors for pagination;
- bounded event-range export with optional integrity inspection;
- transcript extraction from the durable message vocabulary;
- deterministic JSONL event output, with recursive redaction at the export boundary.

The service validates every caller limit, honors a stricter adapter read ceiling,
and never imports a concrete persistence implementation. Cursors include the
initial range, so a cursor cannot be replayed against a different query.

## Evidence

- `sonder_runtime/application/session/query_export.py`
- `sonder_runtime/application/ports/session_repository.py`
- `sonder_runtime/adapters/persistence/session_repository.py`
- `tests/test_remaining_session_query_export.py`

Focused tests cover pagination, sparse filters, range reads, deterministic
ordering, transcript extraction, recursive secret redaction, integrity output,
cursor binding, and adapter-level bound enforcement.
