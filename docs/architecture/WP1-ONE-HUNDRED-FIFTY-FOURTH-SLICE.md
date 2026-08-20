# WP1 One-Hundred-Fifty-Fourth Slice — checklist event adapter ownership

## Boundary

Moved checklist event-sink implementation ownership out of the generic
`task_store` adapter and into the dedicated packaged
`sonder_runtime.adapters.task_events` boundary. `task_store.LegacyChecklistEventSink`
remains an identity-compatible alias for the existing composition root.

The sink continues to publish a copied checklist mapping, preserving the
existing mutation-isolation behavior. This slice is non-server-only and does
not alter `server.py` or the task repository boundary migrated in slice 152.

## Verification

- `python -m pytest tests/test_task_event_sink_adapter.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime` — pass.
- `git diff --check` — pass.
