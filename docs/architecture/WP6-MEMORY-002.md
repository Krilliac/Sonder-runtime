# WP6-MEMORY-002 — Labeled retrieval evaluation

This slice adds a read-only evaluator at
`sonder_runtime.application.memory.retrieval_evaluation`. It measures the
existing recall contract rather than changing it: the production recall path
continues to own the `0.72` default cosine floor, lexical/semantic fallback,
and deterministic MMR selection.

Each labeled case supplies expected relevant IDs, optional contradictory and
stale IDs, returned memories, latency, and context-token cost. The evaluator
reports bounded relevance precision/recall, contradiction rate, stale recall,
95th-percentile latency, and average context cost. Contradiction and stale
labels are kept separate because a returned item can be relevant to a query
while still being unsafe or temporally invalid.

The evaluator caps dataset and per-case sizes, rejects negative latency and
invalid `k`, deduplicates returned IDs per query, and performs no persistence,
embedding, network, or model calls. Formal master-spec checkboxes remain
unchanged; this is evidence for the memory quality-evaluation work only.
