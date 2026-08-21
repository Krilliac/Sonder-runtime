# Append-only compaction service evidence

`CompactionAppendService` verifies an exact immutable source event range before
appending one bounded `compaction.completed` event. It retains structured
facts, decisions, unresolved tasks, artifacts, and tool outcomes, rejects
truncated or identity-mismatched ranges, and never deletes or rewrites source
events.

Focused verification:

```text
python -m pytest -q tests/test_compaction_append_service.py tests/test_wp4_compact001_005.py
```

This proves the append boundary and structured bounds; full production
compaction wiring and formal checklist promotion remain open.
