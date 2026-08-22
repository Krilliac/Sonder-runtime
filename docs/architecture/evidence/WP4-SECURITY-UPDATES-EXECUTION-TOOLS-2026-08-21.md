# Security, updates, execution, and tools composition — 2026-08-21

This batch adds provider-neutral boundaries for:

- opaque credential leases and per-redirect re-authorization, with unsafe
  header/control-character rejection and fail-closed missing providers;
- bounded update state, signed/runtime evidence, explicit authority, verified
  backup, atomic activation, and rollback recovery;
- shared execution-world/containment/output-spill composition and a typed tool
  facade for resource policy, approval, generated catalogs, redaction, and
  truthful receipts.

Evidence includes the application facades, ports, adapters, runtime graph
wiring, and focused tests in `tests/test_security_production_boundary.py`,
`tests/test_update_application_service.py`, and
`tests/test_execution_tools_facade.py`, plus their existing boundary suites.

The combined focused run passed 150 tests with one intentional skip. These are
local contract/composition results only: no real credentials, update, remote
execution, container, or external provider call was made. Platform-specific
receipts and formal verification remain outstanding.
