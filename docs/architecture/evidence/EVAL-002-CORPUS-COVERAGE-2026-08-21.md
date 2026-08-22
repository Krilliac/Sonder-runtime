# EVAL-002 — Durable corpus coverage inventory

## Scope

This slice adds a typed, bounded inventory for the three required evaluation
corpus classes: repository tasks, tool-use tasks, and memory/grounding tasks.
The inventory is an application contract; adapters provide source readers and
the application records only bounded record identities and digests.

## Guarantees

- Every required source class must be represented by a complete report.
- Each record is bounded by encoded bytes; each source is bounded by record
  count and total bytes.
- The inventory digest covers source IDs, source kinds, classifications, record
  IDs, record digests, and byte counts.
- Missing, duplicate, invalid, unreadable, and scan-limited sources receive an
  explicit classification.
- `require_complete()` fails closed and cannot be used to treat partial
  repository/tool/memory coverage as a valid evaluation corpus.

## Evidence

Focused coverage is in `tests/test_eval002_corpus_inventory.py`: complete
three-source coverage, deterministic and content-sensitive digests, missing
source classification, bounded truncation, invalid/read-error handling, and
duplicate-source rejection.

The separately deployed evaluation corpus providers remain outside this slice;
the adapter contract intentionally reports them as incomplete when unavailable.
