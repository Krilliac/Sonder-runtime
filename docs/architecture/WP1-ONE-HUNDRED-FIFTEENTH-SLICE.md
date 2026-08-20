# WP1 one-hundred-fifteenth slice — model capability normalization

## Scope

The pure `_fanout_capabilities` helper in `server.py` normalized explicit
Ollama-compatible catalog records, but its implementation still lived in the
legacy composition root. This slice moves that deterministic transformation to
`sonder_runtime.domain.model_capabilities.fanout_capabilities`.

The server keeps an identity-preserving compatibility alias, while the
fanout non-chat classification and model selection behavior remain unchanged.
No metrics, `unsafe_lab`, or autopilot implementation was changed.

## Verification

- `pytest -q tests/test_model_capabilities.py tests/test_model_fanout.py` — pass.
- `python -m compileall -q sonder_runtime/domain/model_capabilities.py server.py` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `git diff --check` — pass.
