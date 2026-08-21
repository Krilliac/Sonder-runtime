# EXEC-004 durable spill output evidence — 2026-08-21

## Scope

This slice wires the existing typed `SpillStore`/`SpillReference` contract into
the smallest durable execution-output path. `SQLiteSpillStore` owns staged
bytes and committed metadata in SQLite. `DurableExecutionOutput` converts
bounded text output into the existing digest-bound `SpillReference` without
introducing HTTP, MCP, job/session lifecycle, compaction, or selfmod changes.

## Guarantees

- `SpillSpec.max_bytes` is enforced during every write and before text output
  staging; an over-bound write cannot commit partial output.
- Commit computes SHA-256 over the exact stored bytes and returns an immutable
  `ArtifactHandle`.
- Reads verify committed state, digest, declared size, actual size, and the
  recomputed SHA-256 before returning bytes; tampering fails closed.
- Reads require an explicit byte bound, and the reference preserves the output
  owner id, digest, size, MIME type, and bounded preview.
- SQLite persistence is proven by closing and reopening the store in tests.

## Evidence

`tests/test_exec004_durable_output.py` covers reopen persistence, reference
binding, read bounds, write bounds, abort behavior, and payload tampering.

## Remaining boundary

This is the adapter/composition seam for durable spill storage. It does not
modify the existing job registry or claim that every process launcher is
already composed with this adapter; those integrations remain separate slices.
