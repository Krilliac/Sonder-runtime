# Remaining SESSION-002–005: durable repository and crash-safe replay

## Contract

`sonder_runtime/application/session/durable_replay.py` defines the
`AppendOnlySessionRepository` application contract.  The contract exposes
committed append, ordered bounded reads, and integrity inspection only; update
and delete are not part of the port.  The existing SQLite adapter enforces
immutability with database triggers and a per-session sequence/hash chain.

## Reconstruction

`reconstruct_model_visible_request` rebuilds the exact durable request
snapshot, including transport fields, history, options, tools, UI facts,
request identity, turn identity, and a deterministic SHA-256 snapshot digest.
It never consults current provider or UI configuration.

## Crash safety and evidence

`crash_safe_replay` reads the complete bounded stream from sequence one,
requires a valid sequence/hash chain before projection, and rejects a bounded
prefix that cannot prove the tail was reached.  It returns the integrity report,
replayed projection/transcript, request envelope, and recovered watermark.  A
tampered event or unverifiable tail fails closed with `IntegrityFailure`.

Focused evidence: `tests/test_remaining_session_durable_replay.py`.

Formal specification checkboxes are intentionally unchanged.
