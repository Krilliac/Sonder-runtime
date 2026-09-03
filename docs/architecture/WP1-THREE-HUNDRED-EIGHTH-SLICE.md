# WP1 Three-Hundred-Eighth Slice — ensemble synthesis prompts

## Boundary

The ensemble synthesis prompts (`_ensemble_candidate_references`,
`_ensemble_candidate_boundary`, `_ensemble_code_synthesis_prompt`,
`_ensemble_synthesis_prompt`) now live in
`sonder_runtime/domain/ensemble_synthesis.py` as `candidate_references`,
`candidate_boundary`, `code_synthesis_prompt` and `synthesis_prompt`, with the
compact ASCII JSON serialization, the untrusted-reference envelope and both
rule lists unchanged. `server.py` keeps all four root names as
identity-preserving alias imports, so `ensemble_answer` and the natural
ensemble compiler path call the same objects.

## Evidence

- `tests/test_ensemble_synthesis_boundary.py` verifies the four alias identities, the compact ASCII candidate serialization, the untrusted-reference envelope, and both synthesis prompts' authoritative request and terminal cue.
- `python -m pytest -q tests/test_ensemble_synthesis_boundary.py tests/test_ensemble_answer.py tests/test_natural_ensemble_compiler.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
