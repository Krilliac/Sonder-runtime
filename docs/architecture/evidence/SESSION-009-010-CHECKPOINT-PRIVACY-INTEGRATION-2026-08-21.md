# SESSION-009/010 checkpoint and privacy integration — 2026-08-21

This bounded slice connects projection checkpoints and event privacy/retention
contracts to the canonical `SessionRepository` through
`SessionCheckpointPrivacyService` and its typed persistence factory.

- Checkpoints are append-only `session.projection_checkpoint` events with a
  bounded JSON envelope and deterministic digest.
- Reload rejects malformed envelopes, digest changes, and stale source
  sequence/hash values. Re-saving an identical checkpoint is idempotent.
- Retention returns bounded, read-only candidates only for valid privacy class,
  timestamp, retention, and delete rules. Unknown or malformed metadata is
  retained fail-closed; no delete operation is introduced.

Focused validation:

```text
python -m pytest -q tests/test_session_checkpoint_privacy_integration.py tests/test_session_checkpoints.py tests/test_session_privacy.py tests/test_session_repository.py
```

HTTP, MCP, jobs, execution spill, memory, training, data, fleet, evaluation,
update, operations, model, compaction, and selfmod remain outside this slice.
