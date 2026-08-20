# WP1 One-Hundred-Forty-Seventh Slice

## Boundary

Moved explicit SPEC-5 runtime graph assembly from the bootstrap implementation
module into the canonical packaged `runtime_container` adapter. The bootstrap
module remains a compatibility import surface for `Runtime` and
`build_runtime`; model-backend selection, event-sink construction, and clock
construction retain their prior behavior.

## Verification

- `tests/test_runtime_container_adapter.py`: 3 passed.
- Ollama and OpenAI-compatible gateway selection were exercised without
  changing server, transport, or previously migrated ownership boundaries.
- Architecture, requirement-evidence, compile, and `git diff --check` gates
  passed.

This slice moves only generic runtime composition. Runtime configuration,
capability policy, CLI parsing, and application lifecycle remain in their
existing canonical boundaries.
