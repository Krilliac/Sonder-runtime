# WP1 Three-Hundred-Forty-Second Slice — agent path keys

## Boundary

Moved `_agent_created_path_key` from server.py into
`sonder_runtime/domain/agent_path_keys.py` as `created_path_key`.

Canonical path-key normalization for the agent run-created-paths ledger.
Case-folds and separator-normalizes so equivalent paths on Windows all
map to the same key. Stdlib only (os.path).

The root `server._agent_created_path_key` is an identity-preserving alias.

## Evidence

- `tests/test_agent_path_keys_boundary.py` verifies identity alias,
  path normalization, None handling, and separator equivalence.
- `python -m pytest -q tests/test_agent_path_keys_boundary.py` — 4 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python -m compileall -q sonder_runtime/domain/agent_path_keys.py server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
