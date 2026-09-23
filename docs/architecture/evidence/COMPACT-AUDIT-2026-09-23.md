# COMPACT requirement audit — 2026-09-23

Status: audit of the compaction code on `origin/main` at
`494f2397601b784abe82d5131693f16baad709d4` against COMPACT-001 to COMPACT-005
in the master spec and against issue #510 section 1 (lossless archive and
selective context eviction). This record is evidence for the ledger rows added
in the same change. It does not promote any requirement to `verified`.

## Surfaces audited

| Surface | Role |
|---|---|
| `sonder_runtime/application/ports/compaction.py` | Typed port: `SessionHistoryEvent`, `SourceRange`, `CompactionSummary`, `CompactionEvent`, validation |
| `sonder_runtime/application/compaction.py` | Deterministic engine (`CompactionApplicationService`) |
| `sonder_runtime/application/compaction/session_service.py` | `SessionCompactionService`: read range, compact, append, validate persisted summaries, `archive_context` |
| `sonder_runtime/application/session/archive.py` | Tool-output eviction to digest-bound `context.archive.created` references |
| `sonder_runtime/application/compaction/append_service.py` | Structured append boundary |
| `sonder_runtime/application/agents/interactive_lanes.py` | Live lane replay: validated summaries replace their covered range in the provider request |
| `sonder_runtime/domain/context/compaction.py` | Emergency overflow retry: drops the oldest whole turns from one provider request |

## Requirement status on main

| ID | Requirement | Finding on main | Gap |
|---|---|---|---|
| COMPACT-001 | Compaction appends an event and never replaces or deletes raw history | Met by the durable path. `SessionCompactionService.compact` only calls `repository.append`, and the SQLite repository is hash-chained and append-only. The emergency overflow path and lane replay change only the provider request copy. | No gap in raw history. |
| COMPACT-002 | Bind every summary to the exact source range | Met. `SourceRange` carries the session, both sequences, and both endpoint event IDs. `validate_persisted_event` rejects missing, extra, or reordered source events and summaries that are included in their own range. | None. |
| COMPACT-003 | Retain facts, decisions, unresolved tasks, artifacts, tool outcomes, and confidence separately | Partly met. The five structured fields and confidence are kept separately. **Gap 1:** `requirements` and `constraints` are not structured fields. The engine dropped every `message.received`/`message.sent` text event (the real assistant type, `message.emitted`, was kept in full instead) from the summary, so a constraint or requirement on a user message was lost. The lane then replaced the covered range with that summary, so the live provider request lost it too. **Gap 2:** there was no failure category. Failures survived only when they happened to be non-message events. | Fixed in this change (see the evidence document). |
| COMPACT-004 | Evaluate factual retention; allow re-compaction from original events | Partly met. The engine checked only `facts`. Persisted summaries are re-derived from source and compared. **Gap 3:** the engine validated itself. `SessionCompactionService.compact` trusted `engine.validate`, so an injected (for example model-backed) engine could drop failures and still report `valid=True`, and its summary was appended. | Fixed in this change: an independent deterministic retention gate. |
| COMPACT-005 | Do not flatten reasoning, images, tools, or attachments into text | Mostly met. Non-message events are kept as typed modalities with their `event_type` and `modality`. **Gap 4 (issue #510, "evict bulky tool output first"):** every tool result was inlined in full as a modality. A summary could therefore be larger than the events it replaced, and it undid the eviction that `archive_context` had already done. | Fixed in this change: bulky tool output is kept as a digest-bound reference. |

## Issue #510 section 1 checklist

| Goal | Before | After this change |
|---|---|---|
| Preserve requirements and constraints durably | Raw events were durable, but summaries dropped them from live context | Retained as typed modalities, and the gate enforces it |
| Preserve decisions and rationale | Structured `decisions` were kept, and `model.response` was kept in full | Unchanged, and now gated |
| Preserve failure history | Only incidentally | `is_failure` classification. Failures are always retained, with error and status keys kept verbatim even when the output is referenced |
| Keep verified facts | `facts` were validated | Unchanged |
| Keep artifact and code references | The `artifacts` field | Unchanged |
| Evict bulky tool output before compressing reasoning | `archive_context` did this for the uncompacted tail only. Summaries re-inlined the output | Summary references use `reference_*` keys with sha256 and byte count |
| Prefer reference-backed summaries | The range was bound, but tool payloads were copied | Bulky payloads are recoverable through `retrieve_reference`, which verifies the digest |
| Searchable and recoverable | `archive.search` covered raw text only | `search_compacted` marks the covering summary. `recover_source` and `recall_critical` return validated original events |
| Canary for compaction and continuation | None | `tests/test_compaction_continue_canary.py` (8 tests, including a live-lane restart) and `tests/test_compaction_retention_review.py` (31 review tests) |

## Measured RED on main

The canary scenario was run against the unmodified source at the base SHA. It
planted a requirement, a constraint, structured and rationale decisions, three
failed attempts, and two bulky tool outputs:

```text
LOST ['REQ-offline-only', 'CONSTRAINT-no-network']
INLINED ['LINKER-NOISE', 'BULKY-OK']  BYTES 38351
LOSSY_ACCEPTED True
```

A live-lane probe on main produced the same result after compaction:
`CONSTRAINT-KEEP-OFFLINE` was absent from the provider request, and the
24 KB tool payload was replayed inline, giving 24,623 bytes of history. With
this change the same probe gives 909 bytes of history, and the constraint,
decision, and failure are all present.

## Remaining gaps (not addressed here)

- **Emergency overflow retry** (`domain/context/compaction.py`) still drops
  whole old turns from the retry request. It includes an in-band note, but
  failures and decisions in those turns are not recalled. It should use
  validated summaries and references instead.
- **Model-visible retrieval of summary references.** Lanes expose
  `retrieve_archived_tool` for `context.archive.created` IDs. A summary's
  `reference_*` modality can be recovered through
  `SessionCompactionService.retrieve_reference`, but it is not yet a lane tool.
  `interactive_lanes.py` is owned by open PRs #542 and #545.
- **Nested payloads in lane replay.** The lane serializes each modality with
  `json.dumps(dict(payload))`. A source event whose payload contains a nested
  object is frozen to `mappingproxy` by the port, and the lane turn fails with
  `TypeError`. That is fail-closed, but it breaks continuation. This change
  keeps reference payloads flat. Fully retained events with nested payloads
  are still affected until the lane serializer is fixed.
- **Classification is structural.** Constraints, requirements, and decisions
  are recognized only from structured payload keys, and failures from event
  type and status keys. A constraint stated only in free text inside
  `message.received` still collapses into the bound source range. It stays
  recoverable through `recover_source` and `search_compacted`, but it is not in
  the provider view.
- **Long-session continuation.** The lane reads a bounded 256-event tail.
  Open PR #542 owns complete-history paging.
- **Verification.** Hosted CI, a production lane run, and master-spec
  checkbox promotion are still pending, so all five rows stay
  `implemented_unverified`.
