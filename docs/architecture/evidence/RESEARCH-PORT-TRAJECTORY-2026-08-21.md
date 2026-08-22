# Research port: inspectable action/observation trajectories

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

SWE-agent treats trajectories as a primary debugging and evaluation artifact.
OpenHands likewise models agent execution as event-driven steps and streams
events to clients. Sonder already persists tool-call and tool-result session
events, but did not expose a dedicated inspection projection.

Sources: <https://github.com/SWE-agent/SWE-agent/blob/main/docs/usage/inspector.md>
and <https://docs.openhands.dev/sdk/arch/agent>

## Implemented slice

`project_trajectory()` now derives a bounded trajectory from authoritative
session events:

- links `tool.call` actions to `tool.result` observations by call ID;
- represents incomplete calls as pending and unmatched observations as an
  integrity failure;
- exposes hashes and byte counts instead of raw arguments/results;
- supports deterministic JSON and JSONL export for inspection/evaluation.

Evidence: `tests/test_session_trajectory.py`.
