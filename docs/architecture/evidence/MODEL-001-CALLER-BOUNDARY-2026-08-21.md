# MODEL-001 caller migration boundary — 2026-08-21

## Result

No safe non-legacy caller remains for this bounded migration slice. The
packaged application, adapter, and interface layers do not import the flat
`server` module, `bootstrap.legacy_root`, or `bootstrap.legacy_model`.

The sole remaining model-root seam is intentional bootstrap composition:

`bootstrap/app.py` → `bootstrap/legacy_model.py` → `bootstrap/legacy_root.py`

`legacy_model.py` lazily adapts the historical `_serve_target` and
`_make_generate` functions to the typed `ModelTargetResolver` and
`ModelGenerateFactory` contracts consumed by the `ModelGateway`. Removing this
seam requires moving those two policy/transport behaviors out of the root
runtime, not changing an already-safe caller.

The REPL is not counted as a safe caller: it is an explicitly injected legacy
interface (`_LegacyRuntimeProxy`) and changing it would be a separate legacy
surface migration with different interaction and model-selection semantics.

## Next seam

Extract the behavior behind `_serve_target` and `_make_generate` into typed
bootstrap-owned providers, then replace `lazy_legacy_model_provider_factories`
with those providers. Preserve tier resolution, cloud consent, system prompt
composition, generation options, context sizing, and provider error mapping
under the existing `ModelGateway` contract before deleting the compatibility
adapter.

## Evidence

- `tests/test_model001_caller_boundary.py`
- `tests/test_wp1_model_adapter_root_removal.py`
- `tests/test_wp1_root_server_boundary.py`
- `python -m pytest -q tests/test_model001_caller_boundary.py
  tests/test_wp1_model_adapter_root_removal.py
  tests/test_wp1_root_server_boundary.py`
- `python scripts/check_architecture.py`
- `python scripts/check_evidence_documents.py`

This is a boundary/evidence slice; no formal checklist checkbox is marked
verified and no legacy behavior is removed without an equivalent typed owner.
