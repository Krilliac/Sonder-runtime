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

The canary
`test_live_request_compacts_canonical_tool_output_and_recovers_after_restart`
proves one real lane request receives a bounded reference instead of the
bulky tool payload, retains a decision and a prior failure, and recovers the
original payload through a reopened `AgentLaneService`.

The provider is still responsible for its own token accounting and semantic
summary. This slice does not claim factual validation, re-compaction, or
provider-specific tokenizer equivalence; those remain separate COMPACT gates.

The live request path reads a bounded 256-event canonical tail. If that tail
starts after the first session event, the request now fails before archive or
provider assembly: an older event could contain an accepted constraint or
decision, and the selected tail cannot prove otherwise. Older events remain
durable, but full long-session continuation requires a reference-backed
selection or a durable continuation cursor and remains an open COMPACT gap.
Protected facts within a complete selected tail are never silently truncated:
exceeding the 40-message or 32 KiB budget for protected facts raises a
recoverable `ContextHistoryOverflowError`, leaves the lane in
`awaiting_input` with `CONTEXT_HISTORY_OVERFLOW`, and allows an explicit
resume after operator-led compaction. A persisted summary is accepted only
when its exact source range and typed modalities validate, its factual and
structured retention checks pass, and it does not overlap another summary;
malformed, incomplete, or overlapping summaries fail closed. The 257-response
canary and an earlier-decision-plus-257-tool-requests canary prove that an
incomplete tail fails rather than silently losing protected history.
