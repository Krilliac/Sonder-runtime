# EXT-003 live memory-limit wiring — 2026-08-21

The production bootstrap host factory now has direct integration coverage for
the typed extension experiment boundary. A requested `ExperimentLimits` memory
budget reaches the composed `ExtensionHostLimits` unchanged, along with the
child environment, before the host startup handshake.

Evidence:

- `sonder_runtime/bootstrap/app.py` maps the application experiment limit to
  `ExtensionHostLimits` and constructs the native `ExtensionHost`.
- `tests/production/test_extension_composition.py::test_live_bootstrap_host_factory_preserves_memory_limit`
  verifies the live bootstrap path with a deterministic host seam.
- `tests/test_extension_memory_limits.py` covers native enforcement behavior;
  the production test proves the declared budget reaches that adapter boundary.

The full platform matrix and artifact discovery/download remain outside this
slice; EXT-003 is still formally unverified.
