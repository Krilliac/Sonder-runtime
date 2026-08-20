# WP1 ninth migration slice: model transport error adapter

**Status:** Focused verification passed

## Scope

Retire the root `model_transport.py` lazy facade. `ModelCallError` is now used
directly from `sonder_runtime.adapters.model_transport`, with its stable type
identity and public type path owned by the package adapter.

## Completed work

- [x] Rewire the server and model-gateway tests to the package adapter.
- [x] Make the package adapter authoritative for `ModelCallError` identity and
  public type path.
- [x] Remove the root from selfmod and local-package inventories.
- [x] Add the root to the permanent retired-module architecture ratchet.
- [x] Extend the reintroduction regression to cover the root.
- [x] Delete the root facade without adding a replacement shim.

The master-spec requirements remain unchecked; this slice is migration evidence,
not proof of a complete end-state requirement.

Focused verification: `118 passed, 2 skipped`; architecture, evidence, and
staged-diff checks pass.
