# WP2 SESSION-002/008: session repository foundation

This slice adds the application `SessionRepository` port and an isolated
`SQLiteSessionRepository` adapter. It is the first persistence boundary for
session history; operations and memory repositories remain unchanged.

## Contract

Events are immutable records with a per-session, one-based sequence. `append`
allocates the next sequence inside a writer transaction and stores a canonical
JSON payload plus a SHA-256 hash chained to the preceding event. The adapter
rejects empty identities/types and non-JSON payloads.

Every read surface is explicitly bounded: range reads and exports require a
positive limit, search requires a limit, and the adapter's configurable maximum
is enforced. Export is deterministic newline-delimited JSON. Integrity
inspection checks sequence continuity, predecessor hashes, and event hashes and
returns structured issues without repairing or changing history.

The adapter owns only `session_event` in the database path supplied to its
constructor. No existing store, migration registry, projection, or runtime
composition is modified in this foundation slice.

## Deliberate non-goals

Typed lifecycle vocabulary, replay/projections, forking, repair, retention,
redaction policy, and session wiring are subsequent work. Direct SQL mutation
in the focused tamper-detection test is test setup only; production callers
have no update/delete method through the port.

## Redaction (added later)

The redaction non-goal above has since been closed at the write side:
`application/session/capture.py` passes every content field of the events it
appends (request prompt, system text, history, options, UI facts, user and
model messages, tool arguments/results, provider payloads and responses)
through the runtime redactor before the append -- in production the same
`platform.logging.Redactor` the logs and tool audit use (known secret values,
credential assignments, bearer/authorization values, URL credentials, PEM
blocks, known provider key formats), plus whole-value redaction of strings
stored under credential-named keys. The request `snapshot_digest` is computed
over the redacted snapshot, so the stored form is the only form: replay,
export, continuation and integrity checks all read and verify the same
redacted events, and replay stays deterministic. Identity and evidence fields
(ids, tier, tool names, manifests, digests) are never rewritten. Events
appended by other writers (compaction summaries, archive, receipts) are not
covered by this pass; `sessions.db` stays a private, owner-only (`0600`) store
under the same never-share rule as `memory.db`.
