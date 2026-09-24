# Critical-history retention for compaction — 2026-09-23

Status: implementation evidence for issue #510 section 1 and COMPACT-001 to
COMPACT-005. All five ledger rows stay `implemented_unverified`. The audit
that motivated this change is
[COMPACT-AUDIT-2026-09-23.md](COMPACT-AUDIT-2026-09-23.md).

## Change

- `sonder_runtime/application/compaction_retention.py` (new, pure). It
  classifies critical source events:
  - failures: `*.failed`/`*.error` events, error keys, a failed, denied,
    cancelled, or timeout status, `ok`/`success` false, a non-zero integer or
    numeric-string exit code, or a Python traceback on stderr. These are
    checked at the top level and exactly one level inside
    `result`/`output`/`receipt`/`response`;
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
    `reference_sha256`, `reference_byte_count`). The reference is not
    content-free. Its call and status keys (including `success`), and for a
    failure its error, constraint, stderr, and nested `result.*` failure
    values, are kept beside it. Each value is bounded to 1 KiB, and stderr
    keeps its tail.

  Engine validation now fails when critical history is missing.
  `canonical_summary(request, schema=1|2)` re-derives either schema.
- `sonder_runtime/application/compaction/session_service.py`:
  - `compact` runs the independent retention gate after the engine's own
    validation. A lossy engine cannot append a summary.
  - New events carry `summary_schema: 2`.
  - `validate_persisted_event` re-derives legacy (schema 1) and schema 2
    summaries and rejects any summary that differs from its canonical
    projection. An authentic legacy summary that collapsed a constraint is
    replayed as the lossless schema-2 projection re-derived from the same
    original events, so no re-compaction is needed. Re-compacting would be
    rejected by lane replay as an overlapping range. Unknown schemas are
    rejected.
  - New lossless-archive APIs:
    - `recover_source`: the validated original range;
    - `recall_critical`: decisions, failures, constraints, and facts, with
      event identity and hash as provenance;
    - `retrieve_reference`: re-reads the event and verifies its digest;
    - `search_compacted`: a literal, case-sensitive raw-text search in which
      `%` and `_` are not wildcards. It returns the newest matches first and
      marks the newest summary covering each match. The repository's
      oldest-first `LIKE` search is only a superset prefilter; when it could be
      truncated, the service scans the session in keyset pages, and fails
      closed past `max_scan_events`, default 100,000. `recover_source` finds
      summaries the same way, so the newest summary stays reachable beyond the
      adapter read bound. `session_repository.py` is not modified, because
      open PR #542 edits it.

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
- an authentic legacy lossy summary replays the lossless projection;
- a tampered legacy summary fails closed;
- a legacy summary without loss still replays;
- an unknown schema is rejected;
- a tampered reference digest is rejected.

The end-to-end test starts a real `AgentLaneService` lane and plants a
constraint, a decision, failures, and bulky output. It compacts, restarts
every durable component, and continues the lane. The single provider request
contains the constraint, the decision, and both failures. It does not contain
the bulky output, only its reference, and that reference is recoverable after
the restart.

## Review fixes (PR #547)

`tests/test_compaction_retention_review.py` has 31 tests. Before the fixes, 21
of them failed; the other 10 are guards for non-failure cases and the depth
bound.

| Item | Before | After |
|---|---|---|
| P2-a: a lane `tool.result` over 2 KiB with `success: False` | Reference kept only `call_id` and `name`. Removing `success is False` from the classifier left all 7 canary tests passing | `success` is a status field and is kept. That mutation now fails a test |
| P2-b: `{"result": {"status": "failed"}}`, exit code `"1"`, status `denied`, traceback on stderr | Not failures. The capture-shaped result lost its failure signal | Detected at the top level and one level nested. References carry `result.status`, `result.exit_code`, and the tail of `result.stderr` |
| P3-a: more summaries than the read bound (8 summaries at `max_read_limit=5`) | `recover_source(newest)` raised "unavailable". `search_compacted` reported no covering summary | A complete search or keyset scan finds the newest summary |
| P3-b: `100%`, `a_b`, and summaries using up the row budget | `LIKE` wildcards over-matched. Summary rows took the budget (3 of 6 matches returned) | Literal matching; the newest 5 of 6 matches are returned |
| P3-c: remediation for a legacy lossy summary | The error advised re-compaction, which lane replay rejects as an overlap. The lane turn failed | The lossless projection is replayed. The live lane continues with the constraint in its provider request |
| P3-d: `message.emitted` (the canonical assistant type) | Plain assistant chatter was retained as a modality | It collapses like other plain text. Constrained text is kept |
| P3-e: documentation | The reference was described as "content-free" | Wording corrected; rollback note added below |

Mutation checks: removing each of these makes at least one test fail:

- the `success` signal;
- the `success` status field;
- nested inspection;
- numeric-string exit codes;
- the `denied` status;
- traceback detection;
- the stderr tail;
- `message.emitted`.

## Re-review fixes (PR #547)

- **Bounded search memory.** `search_compacted` keeps only the newest
  `limit` matches while it scans. Candidates arrive oldest-first, so it uses
  `deque(maxlen=limit)`. Before this fix, a broad query kept every match of
  the scan in memory (up to 100,000 events). The test
  `test_broad_search_retains_only_the_newest_limit_matches_while_scanning`
  searches 500 matching events with `limit=3` and tracks how many events are
  alive. Before the fix, 500 were alive at peak. After it, the peak is at
  most `limit` plus two pages.
- **Schema-2 golden.**
  `tests/test_compaction_summary_schema_golden.py` pins the exact schema-2
  output for a representative fixture that covers these cases:
  - constrained and plain user and assistant text;
  - structured fields and confidence;
  - rationale;
  - a failed tool;
  - a bulky lane result with `success: False`;
  - a bulky capture-shaped nested failure with a stderr tail;
  - a bulky success;
  - an inline tool completion.

  A prominent comment on `SUMMARY_SCHEMA_VERSION` gives the bump rule. The
  golden fails when any of these three is changed in place: the `success`
  status field, the 2 KiB inline threshold, or `message.emitted`.

## Merge with main after #542 (verified snapshots)

In #542 the live lane builds its context from `read_complete`, which returns
one chain-verified snapshot of the whole session. It passes those verified
events to `archive_verified_context` and to `validate_persisted_event`.

The retention gate, the schema-2 projection, and the lossless replay of
legacy summaries all run inside `validate_persisted_event`. They operate only
on the events the caller passes in and never re-read storage, so the model
still sees only chain-verified events.

`recover_source` now slices its range from `read_complete` when the
repository offers it. The test
`test_recover_source_uses_the_chain_verified_snapshot` alters a plain-chatter
row out of band. That row is not critical, so the summary check alone cannot
catch it. Before this change the altered row was returned. After it, the call
fails with an integrity error.

Ledger rows from this PR were renumbered above the revisions already on
main:

| Requirement | Revision |
|---|---|
| COMPACT-001 | 5 |
| COMPACT-002 | 5 |
| COMPACT-003 | 5 |
| COMPACT-004 | 7 |
| COMPACT-005 | 4 |

## Rollback note

Code before this PR does not know `summary_schema`. It recomputes every
summary with the schema-1 projection. A schema-2 summary written by this PR
differs from that projection, so after a rollback,
`validate_persisted_event` raises "differs from canonical source summary".
Lane turns whose live tail contains such a summary then fail closed, and no
data is lost. To recover after a rollback, those lanes must continue in a new
session, or the rollback must be paired with a forward fix that accepts
schema 2.

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
python -m pytest -q tests/test_compaction_continue_canary.py tests/test_compaction_retention_review.py tests/test_compaction_summary_schema_golden.py tests/test_session_complete_recovery_adversarial.py
73 passed

python -m pytest -q -n 8 <120 compaction/session/context/lane/replay/archive test files, after merging main>
2122 passed, 1 skipped, 1 warning
```

Two earlier parallel runs each had one intermittent failure. The first was
`test_agent_lane_http_wiring.py::test_agent_routes_require_auth_before_service_access[None]`.
That file passes on its own, both with and without this change. The second
failure was not captured. The next three consecutive full runs passed.

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
