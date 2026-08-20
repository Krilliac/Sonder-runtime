# WP1 fifteenth migration slice: NPU manifest and provider adapters

**Status:** Focused verification passed

## Scope

Move `npu_manifest.py` and `npu_providers.py` into the packaged NPU adapter
boundary, rewire the broker/service and test callers, and remove both roots from
the local bundle and nightly source inventories.

Focused verification: `226 passed, 5 skipped`; architecture, evidence, and
staged-diff checks pass.

The root `npu_service.py` and `npu_broker.py` process-facing boundary remains for
the next slice.
