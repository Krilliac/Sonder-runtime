# Durable session compaction integration evidence — 2026-08-21

Status: implementation evidence for the production composition slice; formal
master checklist and `requirements.jsonl` remain unchanged.

## Scope

The application graph now exposes one lazy `SessionCompactionService`. It reads
an exact durable session-event range, converts events into the typed compaction
port, runs the deterministic compaction/retention validator, and appends exactly
one `compaction.completed` event. Source events are never replaced or deleted.

The persisted summary retains structured facts, decisions, unresolved tasks,
artifacts, tool outcomes, confidence, and typed non-text modalities. The
repository is reopened in the test to prove the appended event is restart
visible.

## Implemented paths

- `sonder_runtime/application/compaction/session_service.py`
- `sonder_runtime/application/compaction/__init__.py`
- `sonder_runtime/bootstrap/app.py`
- `tests/test_session_compaction_service.py`

## Verification

```text
python -m pytest -q --basetemp .pytest-compaction-next2 \
  tests/test_session_compaction_service.py \
  tests/test_compaction_append_service.py \
  tests/test_wp3_seam007_compaction.py \
  tests/test_wp4_compact001_005.py \
  tests/production/test_application_session_wiring.py
```

Result: **19 passed**.

Additional checks:

```text
python -m compileall -q sonder_runtime tests
```

Result: **pass**.

## Explicit limitations

This slice proves the durable application composition boundary and does not
claim formal completion of every COMPACT row. Model-generated factual quality,
operator-facing HTTP/MCP/REPL compaction commands, and retention-policy
execution remain separate acceptance work. The formal ledger remains
`planned` until the broader requirement-specific gates are reviewed together.
