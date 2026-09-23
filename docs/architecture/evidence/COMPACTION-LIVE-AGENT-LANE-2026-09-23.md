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
64 passed in 13.90s
```

The canary
`test_live_request_compacts_canonical_tool_output_and_recovers_after_restart`
proves one real lane request receives a bounded reference instead of the
bulky tool payload, retains a decision and a prior failure, and recovers the
original payload through a reopened `AgentLaneService`.

The provider is still responsible for its own token accounting and semantic
summary. This slice does not claim factual validation, re-compaction, or
provider-specific tokenizer equivalence; those remain separate COMPACT gates.

The live request path reads a bounded 256-event canonical tail. Events older
than that tail remain durable, but this slice does not prove they are visible
to a resumed provider request. Full long-session decision/failure retention
requires a reference-backed selection or a durable continuation cursor and
remains an open COMPACT gap. Protected facts within the selected tail are
never silently truncated: exceeding the 40-message or 32 KiB protected-fact
budget raises a recoverable `ContextHistoryOverflowError`, leaves the lane in
`awaiting_input` with `CONTEXT_HISTORY_OVERFLOW`, and allows an explicit
resume after operator-led compaction.
