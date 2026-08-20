# WP3 SEAM-007 — CompactionEngine contract

## Boundary

`sonder_runtime.application.ports.compaction` defines immutable
`SessionHistoryEvent`, `SourceRange`, `CompactionRequest`, `CompactionSummary`,
`CompactionResult`, and `CompactionEngine` values/protocols. A source range is
inclusive and binds both sequence endpoints and event identities, so a summary
cannot silently describe a different history slice. Structured facts,
decisions, unresolved tasks, artifacts, tool outcomes, and typed modality
snapshots remain separate fields.

## Semantics

- Requests copy history into an ordered tuple and recursively freeze JSON-shaped
  payloads.
- Validation rejects missing sessions, duplicate or unordered event identities,
  gaps, cross-session ranges, and endpoint mismatches.
- `compact()` is side-effect free at this boundary. A result contains a new
  `compaction.completed` event that callers may append; it never authorizes
  replacing or deleting raw history.
- Validation is repeatable against the original request, so a later adapter can
  re-compact from original events rather than from a prior summary.

## Scope

This slice adds only the new application port, focused contract tests, and this
evidence document. Existing context modules, repositories, adapters, wiring,
and event persistence are unchanged.

## Evidence

- `tests/test_wp3_seam007_compaction.py` covers immutable snapshots, nested
  payload freezing, source-range validation, append-only result binding,
  identity collision rejection, and protocol shape.
- Focused gate: `python -m pytest -q tests/test_wp3_seam007_compaction.py`.
- No specification checkbox, commit, or push is part of this slice.
