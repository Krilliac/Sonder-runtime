# WP1 Three-Hundred-Thirty-Ninth Slice — runtime update parsing

## Boundary

Moved `_runtime_update_object` from server.py into
`sonder_runtime/domain/runtime_update_parsing.py` as `parse_update_object`.

The function accepts a dict, JSON string, None, or empty string and returns
a validated dict; raises ValueError for non-object inputs. Pure domain logic,
stdlib only (json).

The root `server._runtime_update_object` is an identity-preserving alias via
aliased import.

## Evidence

- `tests/test_runtime_update_parsing_boundary.py` verifies identity alias,
  None/empty passthrough, dict passthrough, JSON decode, and error cases.
- `python -m pytest -q tests/test_runtime_update_parsing_boundary.py` — 7 passed
- `python scripts/check_architecture.py` — silent, exit 0
- `python scripts/check_requirement_evidence.py` — silent, exit 0
- `python scripts/check_error_signals.py` — silent, exit 0
- `python -m compileall -q sonder_runtime/domain/runtime_update_parsing.py server.py` — silent, exit 0
- `git diff --check` — silent, exit 0
