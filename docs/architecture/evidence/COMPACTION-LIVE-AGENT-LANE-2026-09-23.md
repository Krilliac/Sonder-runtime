# Live agent-lane context archive evidence

This slice composes the durable context archive into the provider-facing
`AgentLaneService._request` path. Every live lane request now runs a bounded
canonical session range through `SessionCompactionService.archive_context`
before the `ModelRequest` is built.

The archive policy can replace only `tool.result` and `tool.completed` payloads
with content-free references. User messages, model responses, goals, control
facts, and model/tool failures remain in the source range. A reference can be
resolved through the existing lane retrieval surface, and session references
are verified against the append-only source event and its digest after a
service restart.

## Evidence

```text
python -m pytest -q tests/test_interactive_agent_lanes.py tests/test_session_context_archive.py --basetemp .pytest-compact-live-full
62 passed in 10.82s
```

The canary
`test_live_request_compacts_canonical_tool_output_and_recovers_after_restart`
proves one real lane request receives a bounded reference instead of the
bulky tool payload, retains a decision and a prior failure, and recovers the
original payload through a reopened `AgentLaneService`.

The provider is still responsible for its own token accounting and semantic
summary. This slice does not claim factual validation, re-compaction, or
provider-specific tokenizer equivalence; those remain separate COMPACT gates.
