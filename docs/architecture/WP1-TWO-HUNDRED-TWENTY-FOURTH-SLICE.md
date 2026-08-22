# WP1 Two-Hundred-Twenty-Fourth Slice

## Boundary

Moved standalone-client command-line and environment configuration resolution
from `sonder_client.py` into the packaged `sonder_runtime.adapters.client_config`
adapter. The root `_parse_argv` and `resolve_config` names remain compatibility
delegates, while argv precedence and environment fallback behavior stay intact.

## Evidence

- `tests/test_client_config_adapter.py` verifies packaged ownership, argv
  precedence, environment defaults, and compatibility behavior.
- `python scripts/check_architecture.py` passes.
- `python scripts/check_requirement_evidence.py` passes.
- Focused client configuration tests pass.
