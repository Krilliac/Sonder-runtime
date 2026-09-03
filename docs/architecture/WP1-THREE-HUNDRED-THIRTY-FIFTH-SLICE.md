# WP1 Three-Hundred-Thirty-Fifth Slice — selfmod test commands

## Boundary

Moved `_selfmod_test_commands` (automated modification test command builder) into `sonder_runtime/domain/automation/selfmod_test_commands.py` as `selfmod_test_commands`. Uses only stdlib (Path, sys, os, shlex). The root `_selfmod_test_commands` name is now an identity-preserving alias. Added the packaged path to selfmod.py SENSITIVE_PREFIXES.

## Evidence

- `tests/test_selfmod_test_commands_boundary.py` verifies alias identity and command construction.
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime tests server.py`
- `git diff --check`
