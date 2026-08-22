# SESSION-008 / SEAM-006 — bounded session query and export

This slice adds `SessionQueryEngine` at the application boundary. It consumes
the existing `SessionRepository` port and does not mutate repositories,
projections, checkpoints, or the formal implementation checklist.

## Contract

`query_events` requires a session identity and returns a bounded page. The
cursor is versioned, query-bound, and advances by the durable sequence rather
than by an unbounded in-memory offset. `page_size`, repository scan work, and
export event counts are all capped. Event-type and text filters are applied
without bypassing the repository's ordered range read.

`export_events` emits deterministic JSONL records that preserve session ID,
sequence, event ID, type, timestamp, payload, and hash-chain fields. Each
record can be converted back to the existing `DomainEvent` replay shape;
redaction changes only exported payload values and is marked on the record.
`export_transcript` derives user, assistant, and tool messages from both the
existing replay vocabulary and the durable message vocabulary.

The default `Redactor` is applied recursively before export, including nested
objects and arrays. Integrity inspection remains delegated to the repository
and is returned with event exports when requested. No live configuration,
network transport, or write-side effect is used.

## Evidence

- `tests/test_remaining_session_query_export.py`
- focused query/export tests, architecture and requirement-evidence gates,
  `compileall`, and `git diff --check`

## Deliberate non-goals

This is an application seam, not HTTP wiring, retention deletion, or a change
to the existing SQLite schema. Cross-session/global search is intentionally
outside this bounded session-specific query contract; callers can compose it
through the repository port with an explicit bound.
