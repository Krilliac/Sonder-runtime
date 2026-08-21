# WP1 Two-Hundred-Fifty-First Slice — native tool-call policy boundary

## Boundary

Moved validation and deterministic serialization of one model-native tool
call from `server.py` into the pure domain module
`sonder_runtime.domain.native_tool_policy`. The root retains the historical
constants and `_native_tool_call_decision` wrapper for compatibility, while
model transport and host authorization remain outside this policy boundary.

## Evidence

- `tests/test_native_tool_policy.py` covers valid translation, ambiguity,
  invalid names, and argument bounds.
- Existing server and agent-tool regressions continue to exercise the root
  compatibility wrapper.
- Focused result: **314 passed**.
- Architecture, compile, and diff checks pass.
