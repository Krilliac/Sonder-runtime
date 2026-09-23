# Critical-history retention for compaction — 2026-09-23

Status: implementation evidence for issue #510 section 1 and COMPACT-001 to
COMPACT-005. All five ledger rows stay `implemented_unverified`. The audit
that motivated this change is
[COMPACT-AUDIT-2026-09-23.md](COMPACT-AUDIT-2026-09-23.md).

## Change

- `sonder_runtime/application/compaction_retention.py` (new, pure). It
  classifies critical source events:
  - failures: `*.failed`/`*.error` events, error keys, a failed status,
    `ok: false`, or a non-zero exit code;
  - `constraints` and `requirements`;
  - the five structured fields.

  `critical_retention_problems(events, summary)` is a deterministic gate that
  lists every critical item a summary fails to carry.
- `sonder_runtime/application/compaction.py`. Summary schema 2 changes three
  things:
  - plain conversation text still collapses into the bound source range;
  - critical messages are kept as typed modalities;
  - tool output larger than 2 KiB becomes a flat, digest-bound reference
    (`reference_event_id`, `reference_sequence`, `reference_event_type`,
    `reference_sha256`, `reference_byte_count`). Its call, status, error, and
    constraint keys are kept beside it.

  Engine validation now fails when critical history is missing.
  `canonical_summary(request, schema=1|2)` re-derives either schema.
- `sonder_runtime/application/compaction/session_service.py`:
  - `compact` runs the independent retention gate after the engine's own
    validation. A lossy engine cannot append a summary.
  - New events carry `summary_schema: 2`.
  - `validate_persisted_event` re-derives legacy (schema 1) and schema 2
    summaries. A legacy summary that collapsed a constraint now fails closed
    and asks for re-compaction from the original events. Legacy summaries
    without critical loss still replay. Unknown schemas are rejected.
  - New lossless-archive APIs:
    - `recover_source`: the validated original range;
    - `recall_critical`: decisions, failures, constraints, and facts, with
      event identity and hash as provenance;
    - `retrieve_reference`: re-reads the event and verifies its digest;
    - `search_compacted`: raw-text search that marks the summary covering each
      match.

## Canary

`tests/test_compaction_continue_canary.py` plants:

- a requirement and a constraint on a user message;
- a structured decision and fact;
- a decision in model rationale;
- a bulky failing build (exit code 2);
- a failed tool call and a failed model call;
- a bulky successful tool result;
- small talk.

It compacts the range, continues the session with a new message and another
failure, closes and reopens the SQLite repository, and then asserts:

1. the raw source hashes are unchanged and the integrity chain is valid
   (COMPACT-001);
2. the persisted summary validates against the exact range (COMPACT-002);
3. every planted item is in the provider-facing summary, and plain chatter is
   not (COMPACT-003);
4. bulky output is absent from the summary (under 4 KiB) and is recovered
   byte-exact through `retrieve_reference` (issue #510 eviction and reference
   goals);
5. `recall_critical` returns exactly the planted critical events, with their
   original hashes;
6. `search_compacted` finds compacted failures with the covering summary ID,
   and finds post-compaction failures with none;
7. re-compacting the original events after the restart reproduces the summary
   (COMPACT-004).

Other tests in the file cover these cases:

- a lossy engine that reports itself valid is rejected with no append;
- engine self-validation flags a lossy summary;
- a legacy lossy summary fails closed;
- a legacy summary without loss still replays;
- an unknown schema is rejected;
- a tampered reference digest is rejected.

The end-to-end test starts a real `AgentLaneService` lane and plants a
constraint, a decision, failures, and bulky output. It compacts, restarts
every durable component, and continues the lane. The single provider request
contains the constraint, the decision, and both failures. It does not contain
the bulky output, only its reference, and that reference is recoverable after
the restart.

## Verification (Windows local, Python 3.12.10)

RED: the canary scenario against the unmodified base
`494f2397601b784abe82d5131693f16baad709d4`:

```text
LOST ['REQ-offline-only', 'CONSTRAINT-no-network'] INLINED ['LINKER-NOISE', 'BULKY-OK'] BYTES 38351
LOSSY_ACCEPTED True
2 failed
```

GREEN, the same scenario with this change:

```text
LOST [] INLINED [] BYTES 1560
LOSSY_ACCEPTED False
2 passed
```

```text
python -m pytest -q tests/test_compaction_continue_canary.py
7 passed

python -m pytest -q -n 8 <117 compaction/session/context/lane/replay/archive test files>
2004 passed, 1 warning in 167.99s
```

## Limitations

- Classification is structural. A constraint or decision stated only in free
  message text is not recognized. It stays recoverable from the bound range
  and by search, but it is not in the provider view.
- `reference_*` modalities are not yet a model-callable lane tool, because
  `interactive_lanes.py` is owned by open PRs.
- Lane replay still fails with `TypeError` on fully retained modalities whose
  payloads contain nested objects. That behavior predates this change.
  Reference payloads are flat and avoid it.
- The emergency overflow retry (`domain/context/compaction.py`) still drops
  whole old turns.
- Hosted CI, a production lane run, and master-spec checkbox promotion are
  still pending.
