# WP1 One-Hundred-Seventy-First Slice — location consent policy ownership

## Boundary

Moved the pure `SONDER_LOCATION_CONSENT` opt-in policy out of the root
`server.py` implementation and into the canonical
`sonder_runtime.platform.location_consent` boundary. The root
`_env_location_consent` name remains as a compatibility delegate for the
MCP and REPL surfaces. The policy remains off by default and preserves the
historical affirmative values (`1`, `true`, `yes`, and `on`).

## Verification

- `python -m pytest tests/test_location_consent_policy.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
