# MODEL-001 typed model bootstrap providers — 2026-08-21

## Result

The remaining model bootstrap seam is now behind an injected typed provider
boundary. `LegacyModelBootstrapAdapter` is the only adapter that knows the
historical runtime's private `_serve_target` and `_make_generate` names. It
maps the four-value target tuple to `ModelTarget` and forwards the complete
generation argument/keyword surface unchanged.

`legacy_model.py` remains an explicit compatibility composition point. It
resolves `legacy_root.runtime()` only when no provider or runtime is injected,
and the lazy factory keeps that resolution deferred until the first model
operation. New application-port and adapter modules do not import `server`,
`legacy_root`, or `legacy_model`.

## Preserved semantics

- tier and strictness arguments are forwarded unchanged;
- model, cloud, augmentation, and tier-label values retain their existing
  ordering and meaning;
- generation options including cloud routing, timeout, cancellation, schema,
  and fallback controls are forwarded unchanged;
- `OllamaGateway` still receives the same two callable injection points, so
  HTTP and REPL behavior is untouched by this slice.

## Remaining semantic blocker

Deleting the compatibility adapter is not safe in this bounded slice. The
historical `_serve_target` reads mutable runtime tier configuration, refreshes
live cloud tiers, performs exact live-catalog membership checks, and applies
cloud opt-in policy. `_make_generate` additionally owns transport request
shaping, local versus hosted option differences, thinking controls, K3
fallback, bounded usage metadata, activity accounting, and classified
transport errors. Those dependencies are not currently exposed as typed
ports, and moving only the two function bodies would either change live
routing or duplicate private transport policy. The adapter therefore makes
the seam injectable and testable while retaining the old implementation until
those policy/transport owners can be extracted as separate typed providers.

## Verification

- `python -m pytest -q --basetemp .pytest-model001-typed-2 tests/test_model001_typed_bootstrap.py tests/test_wp1_legacy_root_boundary.py tests/test_model001_caller_boundary.py tests/test_lazy_legacy_model_boundary.py` — **15 passed**
- `python -m pytest -q --basetemp .pytest-model001-regression tests/test_model_gateway_factory.py tests/test_model_gateway_conformance.py tests/test_model_routing.py tests/test_wp1_model_adapter_root_removal.py` — **74 passed, 5 skipped**.
- `python -m compileall -q sonder_runtime tests` — required compile gate.
- `python scripts/check_architecture.py` — required layering and import-boundary gate.
- `python scripts/check_evidence_documents.py` — evidence schema gate.
- `git diff --check` — whitespace gate.

This is migration evidence only. It does not claim that the historical model
implementation has been deleted; that remains a later extraction after a
typed provider can own the policy and transport dependencies directly.
