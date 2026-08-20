# WP1 One-Hundred-Sixtieth Slice — inference telemetry ownership

## Boundary

Moved provider-specific inference telemetry normalization into the canonical
`sonder_runtime.adapters.inference.telemetry` package boundary. The Ollama and
OpenAI-compatible gateways now import that package-owned implementation, while
`sonder_runtime.adapters.inference_telemetry` remains a compatibility export
for existing callers. No server surface or slice-159 model-retry boundary was
changed.

## Verification

- `python -m pytest tests/test_inference_telemetry_ownership.py tests/test_inference_telemetry.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime` — pass.
- `git diff --check` — pass.
