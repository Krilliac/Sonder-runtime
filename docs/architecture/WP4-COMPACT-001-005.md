# WP4 COMPACT-001-005 — application compaction engine

## Boundary

`sonder_runtime.application.compaction.CompactionApplicationService` implements
the existing `sonder_runtime.application.ports.compaction.CompactionEngine`
contract. It accepts an immutable `CompactionRequest` snapshot and returns a
candidate `CompactionResult`; it does not load, rewrite, delete, or append
session events.

## Contract coverage

- **COMPACT-001:** source history is never mutated; the result contains a new
  `compaction.completed` event for an append-only caller-owned write.
- **COMPACT-002:** the result and event bind the inclusive source range and its
  endpoint event identities.
- **COMPACT-003:** facts, decisions, unresolved tasks, artifacts, tool outcomes,
  and confidence remain separate fields.
- **COMPACT-004:** `validate()` compares retained facts with the original source
  range. Calling `compact()` again with that same original request is supported
  and produces a new event identity.
- **COMPACT-005:** supported non-text modalities remain typed
  `SessionHistoryEvent` snapshots in `summary.modalities`; they are not folded
  into summary text.

The engine uses only the port contract and does not modify existing context
modules. Payload fields are read conservatively: structured values must be
non-empty strings, and unknown payload fields are ignored. A provider-specific
tokenizer or model summarizer can be introduced behind the same port later.

## Verification

```text
python -m pytest -q tests/test_wp3_seam007_compaction.py tests/test_wp4_compact001_005.py
python -m compileall -q sonder_runtime/application/compaction.py
python scripts/check_architecture.py
python -m ruff check sonder_runtime/application/compaction.py tests/test_wp4_compact001_005.py
git diff --check
```

No master specification checkbox or evidence ledger entry is changed by this
slice.
