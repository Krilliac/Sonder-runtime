# WP1 Three-Hundred-Forty-Seventh Slice — agent mutation record convenience

## Boundary

Added `mutation_record` to the existing
`sonder_runtime/adapters/agent_work_coverage.py` alongside
`mutation_records`.

Returns the first mutation record for a tool invocation, or a default
empty-path record. Thin wrapper over the existing `mutation_records`
function.

The root `server._agent_mutation_record` is an identity-preserving alias.

## Evidence

- `tests/test_agent_mutation_record_boundary.py` verifies identity alias,
  first record return, unknown tool default, and empty args handling.
- `python -m pytest -q tests/test_agent_mutation_record_boundary.py` — 4 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime/adapters/agent_work_coverage.py server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
