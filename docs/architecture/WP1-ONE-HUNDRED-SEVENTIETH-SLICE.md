# WP1 One-Hundred-Seventieth Slice: Model-sizing domain boundary

## Boundary

Moved the pure Ollama model-tag parameter parser from the root
`sonder_hardware` module into `sonder_runtime.domain.model_sizing`. The root
module now imports the canonical function as a compatibility export, so the
existing hardware recommender and external callers retain their behavior while
the model-size policy has a packaged domain owner.

## Evidence

- `tests/test_model_sizing_domain.py` verifies canonical ownership, the root
  compatibility identity, dense and MoE tags, alias safety, and malformed tags.
- Existing `tests/test_sonder_hardware.py` parser and recommender coverage
  remains applicable through the compatibility export.
- `python scripts/check_architecture.py` passes with zero violations.
- `python scripts/check_requirement_evidence.py` passes.
- `python -m compileall -q sonder_runtime server.py` passes.
- `git diff --check` passes.
