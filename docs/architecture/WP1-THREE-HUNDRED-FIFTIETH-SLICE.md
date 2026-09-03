# WP1 Three-Hundred-Fiftieth Slice — cloud agent tool policy

## Boundary

`_cloud_agent_tool_policy_error` moved from `server.py` into
`sonder_runtime/domain/cloud_agent_tool_policy.py` as
`cloud_agent_tool_policy_error`.

The original function referenced module globals `_CLOUD_AGENT_LOCAL_ONLY_TOOLS`
and `_CLOUD_AGENT_NESTED_MODEL_TOOLS`. The packaged version takes these as
keyword arguments (`local_only_tools`, `nested_model_tools`) with empty-tuple
defaults, keeping the domain layer free of module-level state.

The root name remains as a compatibility delegate that binds the two frozensets
from `server.py` module scope.

Added `sonder_runtime/domain/cloud_agent_tool_policy.py` to
`selfmod.SENSITIVE_PREFIXES` — this is a security policy function that restricts
tool access in hosted agent runs.

Two error-signal baseline entries for `return_literal_prefix` in this function
were updated to track the signals at their new path and scope.

## Evidence

- `tests/test_cloud_agent_tool_policy_boundary.py` verifies delegate wiring,
  local-only refusal (not bypassed by `unsafe`), nested-model refusal
  (bypassed by `unsafe`), allowed-tool passthrough, and empty-sets-allow-all.
- `python -m pytest -q tests/test_cloud_agent_tool_policy_boundary.py` — 7 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_error_signals.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime tests` — silent, exit 0
- `git diff --check` — silent, exit 0
