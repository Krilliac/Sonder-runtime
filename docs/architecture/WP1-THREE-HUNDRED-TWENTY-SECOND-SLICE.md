# WP1 Three-Hundred-Twenty-Second Slice — autopilot command programs

## Boundary

The extraction of the programs an autopilot command list would run
(`_autopilot_command_programs`) now lives in
`sonder_runtime/domain/automation/command_programs.py` as `command_programs`,
with the basename-only naming and the invalid marker unchanged. `server.py`
keeps `_autopilot_command_programs` as an identity-preserving alias import,
so the autopilot ledger rendering calls the same object.

## Evidence

- `tests/test_autopilot_command_programs_boundary.py` verifies the alias identity, basename extraction from JSON and object shapes, and every malformed-list case.
- `python -m pytest -q tests/test_autopilot_command_programs_boundary.py tests/test_server_helpers.py -k 'boundary or autopilot'`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
