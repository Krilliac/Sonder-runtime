# WP1 One-Hundred-Forty-Ninth Slice — Model Gateway Factory Ownership

## Boundary

Moved bootstrap's model-backend normalization and gateway construction into
`sonder_runtime.adapters.model_gateway_factory`.  The bootstrap private name
`_build_model_gateway` remains an identity-compatible alias, while the
composition root delegates through the packaged factory.

## Invariants

- Ollama remains the default backend.
- `openai`, `openai-compatible`, `llamacpp`, and `vllm` retain their existing
  OpenAI-compatible selection behavior, including whitespace/case handling.
- Existing bootstrap callers can still import and monkeypatch the private
  selector by identity.
- Existing gateway implementations and their consent boundaries are unchanged.

## Evidence

- `python -m pytest tests/test_model_gateway_factory.py tests/test_openai_compat_gateway.py -q` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `python -m compileall -q sonder_runtime server.py` — pass.
- `git diff --check` — pass.
