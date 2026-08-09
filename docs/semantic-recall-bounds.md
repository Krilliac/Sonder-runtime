# Semantic recall candidate bounds

Semantic recall scores a newest-first, project-scoped window of successful
interactions. This is a deliberate retrieval-quality policy: recent solutions
are more likely to describe the current repository, runtime, and embedding
space than equally similar older solutions. Ranking within the window remains
cosine similarity first, then candidate-window order (timestamp and interaction
id) for exact ties.

The storage adapter applies outcome, session, project, and embedding-space
filters before the window. Enumeration stops at 512 rows, 8 MiB of decoded
candidate data, or 500 ms. A request cancellation uses the same SQLite progress
boundary and reports `cancelled`. `recall_page()` reports `incomplete`, a
termination reason, and a validated opaque `next_cursor`; passing that cursor
retrieves the next older window deterministically. The legacy `recall()` API
continues returning the same list shape and scores only the first bounded
window.

The cursor is a canonical encoding of the exclusive `(timestamp, interaction
id)` boundary. It rejects malformed or non-canonical values, is deterministic
for an unchanged database, and remains insert-safe even if SQLite reuses
rowids: newer rows do not move an older page boundary. It is not an
authorization token or database snapshot; deletes or outcome/project edits
between page requests are reflected normally.

The design uses `(project, timestamp, id)` and `(timestamp, id)` indexes plus
the existing outcome indexes. Adding those indexes is an idempotent
schema-stamp migration over existing columns and requires no content or
embedding backfill. If exact global nearest-neighbor recall becomes a
requirement, add a versioned vector index and backfill it before removing
incomplete evidence; an arbitrary SQL `LIMIT` must not be represented as
complete global ranking.
