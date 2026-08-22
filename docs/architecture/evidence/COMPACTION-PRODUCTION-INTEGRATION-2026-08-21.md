# Durable session compaction integration evidence — 2026-08-21

Status: implementation evidence for the production composition slice; formal
master checklist and `requirements.jsonl` remain unchanged.

## Scope

The application graph now exposes one lazy `SessionCompactionService` backed by
the canonical session repository. It reads an exact durable session-event range,
converts events into the typed compaction port, runs the injected (or
deterministic default) compaction/retention validator, and appends exactly one
`compaction.completed` event. Source events are never replaced or deleted.

The persisted summary retains structured facts, decisions, unresolved tasks,
artifacts, tool outcomes, confidence, and typed non-text modalities. Invalid
confidence values fail closed before any append. The repository is reopened
through a newly built canonical application to prove the appended event,
structured payload, and hash-chain integrity are restart visible.

## Implemented paths

- `sonder_runtime/application/compaction/session_service.py`
- `sonder_runtime/application/compaction.py`
- `sonder_runtime/application/compaction/__init__.py`
- `sonder_runtime/bootstrap/app.py`
- `tests/test_session_compaction_service.py`
- `tests/test_wp4_compact001_005.py`
- `tests/production/test_application_session_wiring.py`

## Verification

```text
python -m pytest -q --basetemp .pytest-compaction-next2 \
  tests/test_session_compaction_service.py \
  tests/test_compaction_append_service.py \
  tests/test_wp3_seam007_compaction.py \
  tests/test_wp4_compact001_005.py \
  tests/production/test_application_session_wiring.py
```

Result: **21 passed**.

Additional checks:

```text
python -m compileall -q sonder_runtime tests
```

Result: **pass**.

The canonical production composition test covers the five requirements as
follows:

- COMPACT-001: raw source events remain present and append-only integrity is
  valid after the compaction event is written.
- COMPACT-002: the persisted source range includes session, sequence, and both
  endpoint event identities.
- COMPACT-003: facts, decisions, unresolved tasks, artifacts, tool outcomes,
  and confidence are persisted as separate fields.
- COMPACT-004: factual validation is run before append; the original range is
  read-only and remains available for another compaction.
- COMPACT-005: tool and image events remain separate typed modality records,
  including their event identities and payloads.

## Explicit limitations

This slice proves the durable application composition boundary. Model-generated
factual quality, operator-facing HTTP/MCP/REPL compaction commands, and
retention-policy execution remain separate acceptance work. The formal master
specification checkboxes remain unchanged; the evidence ledger records this
slice as implemented but unverified until that independent checklist promotion
is authorized.
