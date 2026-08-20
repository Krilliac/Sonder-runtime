# WP1 Two-Hundred-Forty-Seventh Slice — agent mutation policy boundary

## Boundary

Moved the pure agent tool-invocation mutation predicate and its immutable tool
policy set from root `server.py` to
`sonder_runtime.domain.agent_mutation_policy`. The root
`server._WORK_MUTATION_TOOLS` and `server._agent_tool_mutates` names remain
identity-preserving compatibility aliases. Dispatcher authorization,
workspace path confinement, tool execution, and verification policy remain in
`server.py` and are outside this slice.

## Evidence

- `tests/test_agent_mutation_policy.py` verifies default behavior, opt-in
  mutation flags, unknown tools, and root alias identity.
- `python -m pytest tests/test_agent_mutation_policy.py tests/test_agent_dispatch_dev_tools.py -q`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
