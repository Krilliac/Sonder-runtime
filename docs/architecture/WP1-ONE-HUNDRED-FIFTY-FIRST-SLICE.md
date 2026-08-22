# WP1 one-hundred-fifty-first slice — interaction footer policy ownership

## Scope

The pure `_trailing_interaction_id` response-footer parser previously lived in
`server.py`. This slice moves its implementation to
`sonder_runtime.domain.interaction_footer` while preserving the identity-
compatible `server._trailing_interaction_id` alias. The parser continues to
treat the footer delimiter as authoritative and keeps interaction identifiers
opaque rather than assuming the current store format.

## Verification

- Focused interaction-footer tests — 4 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
