# WP1 Two-Hundred-Ninety-Seventh Slice — agent decision parsing

## Boundary

The pure parser that recovers an agent's JSON decision from a model reply
(`_extract_agent_json`) now lives in
`sonder_runtime/domain/agents/decision_parsing.py` as `extract_agent_json`,
with the fence stripping, the direct `json.loads` attempt, the balanced-brace
scan that ignores braces inside JSON strings, and the `ValueError` on
truncated or absent JSON unchanged. `server.py` keeps `_extract_agent_json`
as an identity-preserving alias import, so the agent loop, the autopilot
planner and the goal decomposer call the same object.

The callers deliberately did not move: they own the model round-trip and the
decision-repair re-prompt, which belong to the application boundary.

## Evidence

- `tests/test_agent_decision_parsing_boundary.py` verifies the root alias identity, plain and fenced JSON, prose framing with braces inside strings, and the `ValueError` cases.
- `python -m pytest -q tests/test_agent_decision_parsing_boundary.py tests/test_agent_json_robustness.py tests/test_agent_tools.py`
- `python scripts/check_architecture.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
