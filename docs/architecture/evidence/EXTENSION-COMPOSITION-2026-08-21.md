# Extension composition wiring — 2026-08-21

The production composition root now exposes lazy singleton factories for the
bounded `ExtensionRegistry` and `EphemeralExperimentManager` services.

- `sonder_runtime/bootstrap/app.py` constructs the adapter-backed host factory
  only when the experiment manager is first requested.
- `build_application(..., extension_startup_authority=...)` is the explicit
  startup authority seam. If omitted, experiment startup is denied.
- The application extension modules contain no adapter imports; the child host
  remains an injected protocol boundary.
- The optional `Application` fields are appended after existing fields, so
  existing positional construction remains compatible.

Verification:

```text
python -m pytest -q tests/production/test_extension_composition.py tests/test_extension_registry.py tests/test_extension_experiments.py tests/test_extension_host.py
```

Expected result: all focused extension and composition tests pass. The
architecture checker and compilation gate are also required before this slice
is accepted.
