# WP1 Two-Hundred-Thirty-Seventh Slice — service-state metric projection

## Boundary

Moved the stable numeric process-state projection used by lifecycle metrics
from the web adapter into `sonder_runtime.application.lifecycle`. The service
state machine, transition validation, dependency tracking, and canonical
ownership remain in `sonder_runtime.platform.service_state`. The web adapter's
`_state_number` name remains an identity-preserving alias, and the root
`sonder_service_state` module remains an identity-preserving alias to the
packaged service-state module.

## Evidence

- `tests/test_service_state_application_boundary.py` verifies application
  ownership, all numeric projections, the web compatibility alias, and root
  service-state identity.
- `python -m pytest -q tests/test_service_state_application_boundary.py tests/test_service_state_ownership.py tests/production/test_service_state.py`
- `python scripts/check_architecture.py`
- `python scripts/check_requirement_evidence.py`
- `python -m compileall -q sonder_runtime sonder_service_state.py`
- `git diff --check`
