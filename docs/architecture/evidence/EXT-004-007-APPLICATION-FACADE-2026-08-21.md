# EXT-004–007 application facade evidence

Date: 2026-08-21

Added `ExtensionApplicationFacade` as the typed application-facing boundary
for extension operations. The bootstrap composition root exposes it as a
lazy singleton alongside the registry and ephemeral experiment manager.

The HTTP interface exposes administrator-scoped, machine-readable routes:

- `GET /v1/extensions` for registry health, quarantine, and repair diagnostics;
- `GET /v1/extensions/experiments/<id>/inspect`;
- `POST /v1/extensions/experiments/define`;
- `POST /v1/extensions/experiments/<id>/start`, `/stop`, and `/delete`.

The CLI interface exposes the same operations through the injected
`ExtensionCommand`. Both interfaces construct an explicit typed
`ExtensionAuthority`; no operation infers authority from object access. The
bootstrap default remains fail-closed for process startup, so HTTP/CLI start
cannot bypass the configured startup authority.

The response contract states `persistence: in-memory-only` and
`promotion: not-supported`. The facade does not install artifacts, write a
registry, persist definitions, or promote an experiment.

Verification:

`python -m pytest -q tests/test_extension_facade.py tests/test_extension_http_integration.py tests/test_wp1_http_facade.py tests/test_spec5_interfaces.py tests/test_wp1_http_runtime_injection.py tests/production/test_extension_composition.py --basetemp .pytest-ext-interfaces`

**55 passed**. This includes live HTTP handler requests for health, define,
inspect, start, and stop. Compileall passed. The architecture checker reports one
pre-existing bootstrap selfmod violation outside this slice; the extension
HTTP facade itself has no network-module violation.
