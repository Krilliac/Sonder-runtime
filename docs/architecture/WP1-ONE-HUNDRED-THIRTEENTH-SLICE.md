# WP1 one-hundred-thirteenth slice — autopilot port ownership

## Scope

The authoritative master specification identifies ARCH-001, ARCH-002, and
ARCH-003 as requiring one implementation path, removal of root business
ownership, and removal of strangler services. The autopilot implementation was
already packaged under `sonder_runtime.adapters.persistence.autopilot_store`,
but its `AutomationRepository` port adapter still lived in the generic
`strangler_services.py` module.

This slice moves that adapter to
`sonder_runtime.adapters.persistence.autopilot_repository.AutopilotRepository`
and wires the composition root and regression tests to the new owner. The
immutable `migrations/autopilot/0001_baseline.py` boundary and its root
`autopilot_store.py` compatibility alias are intentionally unchanged.

## Verification

- `pytest -q tests/test_legacy_automation_repository.py tests/production/test_composition_root.py` — 18 passed.
- `python -m compileall -q sonder_runtime/adapters/persistence/autopilot_repository.py sonder_runtime/adapters/strangler_services.py sonder_runtime/bootstrap/app.py` — pass.
- `python scripts/check_architecture.py` — pass.
- `python scripts/check_requirement_evidence.py` — pass.
- `git diff --check` — pass.

The focused pytest run emitted only the known non-fatal Windows pytest-cache
permission warning. No `server.py`, `unsafe_lab`, or metrics files were
changed by this slice.
