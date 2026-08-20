# WP1 one-hundred-forty-third slice — hosted-tier opt-in policy

## Scope

The pure `_cloud_disabled_message` policy text previously lived in
`server.py`. This slice moves ownership to the canonical domain module
`sonder_runtime.domain.cloud_access`; `server._cloud_disabled_message` remains
an identity-preserving compatibility alias. No server state, transport,
adapter, or prior migration implementation changed.

## Verification

- Focused cloud-policy tests — 2 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
