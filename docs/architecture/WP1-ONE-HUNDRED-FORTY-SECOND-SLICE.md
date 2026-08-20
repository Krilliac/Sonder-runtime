# WP1 one-hundred-forty-second slice — CLI input adapter ownership

## Scope

The SPEC-5 startup argument parser previously lived in `bootstrap.main`.
This slice moves ownership to the canonical packaged adapter
`sonder_runtime.adapters.cli_options`, while `bootstrap.main.parse_args`
remains an identity-preserving compatibility import for existing entry points.
No runtime graph construction, capability policy, model gateway, repository,
workflow, or server code changed.

## Verification

- Focused CLI adapter tests — 6 passed.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning.
