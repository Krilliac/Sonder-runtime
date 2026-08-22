# WP1 one-hundred-forty-fourth slice — runtime configuration adapter ownership

## Scope

Startup configuration normalization previously lived in the bootstrap layer:
`RuntimeConfig` was defined by `bootstrap.container`, while
`build_config_from_env` was defined by `bootstrap.main`. This slice moves both
to the canonical packaged adapter `sonder_runtime.adapters.runtime_configuration`.
The bootstrap modules retain identity-compatible imports, so existing entry
points and composition-root callers continue to behave the same way.

The adapter accepts an explicit environment mapping for deterministic tests,
while its default still reads the process environment exactly once at startup.
No server, gateway, repository, tool, event, workflow, evaluation,
inspection, capability, preference, or CLI implementation was changed.

## Verification

- Focused runtime-configuration tests — 3 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run may emit the known non-fatal Windows pytest-cache
permission warning.
