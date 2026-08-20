# WP1 one-hundred-fiftieth slice — environment option ownership

## Scope

The live integer environment-option parser previously lived in `server.py`.
This slice moves it to `sonder_runtime.platform.environment_options`, while
preserving the identity-compatible `server._env_int_option` alias and live
environment behavior.

## Verification

- Focused environment-option tests — 7 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
