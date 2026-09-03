# WP1 Three-Hundred-Tenth Slice — runtime model binding

## Boundary

The checks that bind a local tier to an installed catalog model
(`_runtime_model_is_installed`, `_runtime_model_capability_error`) now live in
`sonder_runtime/domain/runtime_model_binding.py` as `model_is_installed` and
`model_capability_error`, with the `:latest` tag semantics and the vision-tier
exception unchanged; the capability check imports `nonchat_reason` from the
packaged fanout policy directly. `server.py` keeps both root names as
identity-preserving alias imports, so the runtime policy command and the HTTP
serve layer (which reaches the capability check through the root name) call
the same objects.

## Evidence

- `tests/test_runtime_model_binding_boundary.py` verifies the alias identities, installed matching under Ollama's tag rules, and the capability mismatch for embedding, vision, chat and unknown records including the vision-tier exception.
- `python -m pytest -q tests/test_runtime_model_binding_boundary.py tests/test_runtime_policy_server.py`
- `python scripts/check_architecture.py`
- `python scripts/check_error_signals.py`
- `python -m compileall -q sonder_runtime server.py`
- `git diff --check`
