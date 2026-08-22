# WP1 One-Hundred-Fifty-Third Slice — Control Timeout Policy Ownership

## Boundary

Moved the pure interactive `/run` and `/runproject` timeout parser from
`server.py` into `sonder_runtime.domain.control_timeout`. The root module keeps
the identity-compatible `_parse_control_timeout` alias used by existing control
command callers.

## Invariants

- Empty arguments still use the eight-second default.
- Parsed values remain clamped to the existing one-to-sixty-second bounds.
- Invalid values retain the command-specific usage message and tuple shape.
- No execution, subprocess, or command-dispatch behavior changed.

## Evidence

- `python -m pytest tests/test_control_timeout.py tests/test_server_helpers.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
