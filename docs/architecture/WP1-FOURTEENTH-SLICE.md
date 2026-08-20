# WP1 fourteenth migration slice: NPU contract adapter

**Status:** Focused verification passed

## Scope

Move the pure NPU response/manifest contract from root `npu_contract.py` into
`sonder_runtime.adapters.accelerators.npu.contract`, rewire the NPU service,
broker, manifest, provider, server, and tests, and update package and retired-root
inventories.

Focused verification: `218 passed, 5 skipped`; architecture, evidence, and
staged-diff checks pass.

The NPU service/broker/process boundary remains intentionally separate for the
next migration slice.
