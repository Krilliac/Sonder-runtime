# WP1 HTTP runtime injection

## Scope

`sonder_runtime/interfaces/http/serve.py` is a compatibility HTTP adapter for
the existing server-backed route surface. This slice removes its direct
legacy-root import without changing route ownership or modifying the REPL and
entrypoint adapters.

## Contract

- `configure_legacy_runtime(runtime)` is the sole composition hook for the
  historical runtime used by HTTP routes.
- The adapter keeps its established `server.*` route calls behind a small
  injected namespace, so existing server-backed behavior is preserved.
- Access before configuration raises the application-level
  `DependencyUnavailable` error; there is no partially initialized fallback.
- The adapter performs no `importlib` lookup, `sys.modules` lookup, or other
  hidden runtime discovery.
- The shared structured-output bound is read lazily after injection, allowing
  the module to import safely before composition.

## Evidence

`tests/test_wp1_http_runtime_injection.py` proves source-level absence of the
direct import and hidden discovery, fail-closed access, explicit injection,
and delegation through existing server-backed helpers.

Validation commands for this slice:

```text
python -m pytest -q tests/test_wp1_http_runtime_injection.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
python -m compileall -q sonder_runtime
git diff --check
```

The formal master-spec checkboxes are intentionally unchanged.
