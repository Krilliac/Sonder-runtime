# Semantic recall candidate bounds

Semantic recall scores a newest-first, project-scoped window of successful
interactions. This is a deliberate retrieval-quality policy: recent solutions
are more likely to describe the current repository, runtime, and embedding
space than equally similar older solutions. Ranking within the window remains
cosine similarity first, then newest interaction for exact ties.

The storage adapter applies outcome, session, project, and embedding-space
filters before the window. Enumeration stops at 512 rows, 8 MiB of decoded
candidate data, or 500 ms. A request cancellation uses the same SQLite progress
boundary and reports `cancelled`. `recall_page()` reports `incomplete`, a
termination reason, and an exclusive `next_cursor`; passing that cursor
retrieves the next older window deterministically. The legacy `recall()` API
continues returning the same list shape and scores only the first bounded
window.

The cursor is deterministic for an unchanged database and remains insert-safe:
newer rows do not move an older page boundary. It is not a database snapshot;
deletes or outcome/project edits between page requests are reflected normally.

The design uses SQLite rowid keyset pagination plus the existing interaction,
project, and outcome indexes. It does not require a schema migration or vector
backfill. If exact global nearest-neighbor recall becomes a requirement, add a
versioned vector index and backfill it before removing incomplete evidence;
an arbitrary SQL `LIMIT` must not be represented as complete global ranking.
