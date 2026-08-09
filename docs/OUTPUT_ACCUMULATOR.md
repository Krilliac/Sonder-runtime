# Bounded output accumulator

`sonder_runtime.application.output_stream` is a host-internal foundation for
collecting long-running agent, model, or tool text without allowing an
unbounded transcript to grow.

Each accumulator owns one typed `OutputStreamId`. Chunks start at sequence zero
and advance monotonically. New mutations use a revision compare-and-swap;
re-sending the exact bytes for an accepted sequence is idempotent, while a
different replay is rejected. Chunk bytes, total bytes, chunk count, and preview
bytes all have configurable limits below hard ceilings. Exceeding a content cap
fails the stream terminally without accepting the offending chunk.

Snapshots contain counters, terminal state, the SHA-256 of all accepted UTF-8
bytes, and a bounded preview passed through a required host redactor. The
redactor receives a bounded source window with 4 KiB of lookahead beyond the
display limit, so a credential crossing the display boundary is redacted before
the result is UTF-8-safely truncated. Redaction runs after releasing the stream
lock, so a slow or re-entrant host callback cannot block state mutation.

Exact replay retains only each chunk's byte length and SHA-256, never a second
copy of the raw output. While a stream is open it holds at most the bounded
preview-plus-lookahead source. On finalization or failure that raw source is
discarded and only the redacted terminal preview remains. Callers should release
the accumulator after consuming its terminal snapshot; there is no global
registry retaining streams. Terminal events contain metadata and hashes, never
output or previews.

This foundation intentionally has no MCP/API surface, executor, network or
cloud path, agent registration, or SQLite persistence. Restart-safe persistence
would require a dedicated repository and transactional state/event ownership;
none of the current domain stores has matching ownership semantics, so this
slice does not overload them.
