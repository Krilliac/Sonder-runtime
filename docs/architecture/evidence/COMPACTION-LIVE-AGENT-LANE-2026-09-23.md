# Live agent-lane context archive evidence

This slice composes the durable context archive into the provider-facing
`AgentLaneService._request` path. Every live lane request now runs a bounded
canonical session range through `SessionCompactionService.archive_context`
before the `ModelRequest` is built.

The archive policy can replace only `tool.result` and `tool.completed` payloads
with content-free references. User messages, model responses, goals, control
facts, and model/tool failures remain in the source range. Model responses are
treated as protected because their rationale may carry a decision even when
the event is not semantically classified. A reference can be
resolved through the existing lane retrieval surface, and session references
are verified against the append-only source event and its digest after a
service restart.

## Evidence

```text
python -m pytest -q tests/test_interactive_agent_lanes.py tests/test_session_context_archive.py --basetemp .pytest-compact-live-full
67 passed in 9.04s

python -m pytest -q tests/test_interactive_agent_lanes.py tests/test_session_compaction_service.py tests/test_compaction_append_service.py
64 passed in 24.68s
```

Adversarial recovery coverage also proves mid-page hash and sequence
corruption fail closed, a 10,001-event history exceeds the bounded recovery
limit, per-event payloads cannot exceed 8 MiB, and a 257-event history with
300 KiB payloads per event exceeds the 64 MiB total recovery ceiling before
page materialization. Live continuation surfaces each bound as recoverable
overflow. Concurrent appends are read from one SQLite snapshot, so preflight
and materialization cannot observe different page sizes.
The combined focused session, replay, compaction, lane, and production wiring
suite passes with 103 tests.

The canary
`test_live_request_compacts_canonical_tool_output_and_recovers_after_restart`
proves one real lane request receives a bounded reference instead of the
bulky tool payload, retains a decision and a prior failure, and recovers the
original payload through a reopened `AgentLaneService`.

The provider is still responsible for its own token accounting and semantic
summary. This slice does not claim factual validation, re-compaction, or
provider-specific tokenizer equivalence; those remain separate COMPACT gates.

The live request path now reads a complete canonical session history through
bounded adapter pages, verifies the sequence and hash chain before provider
assembly, and then applies the existing selective archive policy. A bounded
recovery limit remains enforced; unavailable or tampered history fails closed.
This allows long-session continuation to retain an earlier requirement,
decision, or failure that would have been omitted by the old 256-event tail.
Protected facts within a complete selected tail are never silently truncated:
exceeding the 40-message or 32 KiB budget for protected facts raises a
recoverable `ContextHistoryOverflowError`, leaves the lane in
`awaiting_input` with `CONTEXT_HISTORY_OVERFLOW`, and allows an explicit
resume after operator-led compaction. A persisted summary is accepted only
when its exact source range and typed modalities validate, its factual and
structured retention checks pass, and it does not overlap another summary;
malformed, incomplete, or overlapping summaries fail closed. The cross-page
recovery canary and an earlier-decision-plus-257-tool-requests canary prove
that omitted tail history is recovered rather than silently lost.

## Adversarial recovery hardening (2026-09-23)

`tests/test_session_complete_recovery_adversarial.py` adds 29 canaries for
`SQLiteSessionRepository.read_complete`: exact page-boundary counts (0-6
events at page size 2), a bound equal to a full-page multiple, one event past
it, invalid bounds, head truncation, gaps at and across page boundaries, a
displaced sequence, a `previous_hash` cycle, payloads swapped across pages, an
event grafted from another session, interleaved sessions, an append forced
between pages, concurrent appenders against repeated recovery, recovery after
restart with a differently sized adapter page, and refusal after close.

Defect found and fixed: the recovery cursor advanced by the count of rows
read (`sequence >= next`), so a displaced row made a page re-read an already
recovered sequence. A within-bound corrupt history (sequences 1,2,4,5,9,
`max_events=5`) was misreported as a recovery-bound overflow instead of an
integrity failure. The RED run reported `1 failed, 28 passed` with actual
message `session history exceeds recovery bound`. The cursor is now keyset
(`sequence > last observed`) and each page is chain-verified before the next
page is fetched.

The append-between-pages canary proves the writer is still blocked after the
first page (SQLite rollback-journal shared lock held by the deferred read
transaction), the recovered snapshot is exactly sequences 1-5, and the late
event becomes visible, correctly chained, on the next recovery.

Known limitation, pinned by a test: deleting the newest events leaves a valid
shorter chain. Without an external head anchor, tail truncation is not
detectable by the hash chain alone.

```text
python -m pytest -p no:cacheprovider -q tests/test_session_complete_recovery_adversarial.py
1 failed, 28 passed   (before the cursor fix)
29 passed             (after the cursor fix)

python -m pytest -p no:cacheprovider -q <76 session/compaction/replay/lane/control-plane/wp4 files>
673 passed in 128.81s
```

## Review follow-up: verified handoff and legacy payloads (2026-09-23)

Independent review of head `c05d35ec` reported two P3 items; both are fixed.

P3-a, unverified second read: the live lane verified history with
`read_complete` but passed only its sequence range to `archive_context`,
which re-read the range with an unverified `read_range`. The model-bound
events therefore came from a second read, not the verified snapshot. The lane
now calls `SessionCompactionService.archive_verified_context`, which archives
the verified tuple as-is (after checking it starts at sequence 1 and is
contiguous). The same change removes a related production defect: bootstrap
wires `SessionCompactionService(repo)` with its default 1,000-event bound, so
`archive_context` raised `source range exceeds the service bound` for any
session between 1,001 and 10,000 events despite complete recovery succeeding.

P3-b, append cap applied to verification: the 8 MiB per-event cap lived in
`_canonical_payload`, which is also used to verify stored events, so a legacy
event over 8 MiB made `inspect_integrity` raise instead of reporting and
blocked continuation as an integrity failure. The cap now applies only on
append (`_bounded_append_payload`); reads keep the 64 MiB total guard in
`read_complete`.

RED before the fixes (4 new tests):

```text
test_live_request_model_context_is_the_verified_snapshot_not_a_second_read
  AssertionError: 'FORGED after verification' reached model history
test_live_request_with_production_default_compaction_bound_recovers_long_history
  SessionCompactionError: source range exceeds the service bound
test_legacy_event_over_append_cap_remains_recoverable_and_reportable
test_tampered_legacy_oversized_event_is_reported_not_raised
  ValueError: payload exceeds the session event byte bound
4 failed
```

GREEN after the fixes:

```text
python -m pytest -p no:cacheprovider -q <the 4 tests plus 2 name-matched neighbours>
6 passed

python -m pytest -p no:cacheprovider -q <76 session/compaction/replay/lane/control-plane/wp4 files>
677 passed in 578.78s
```
