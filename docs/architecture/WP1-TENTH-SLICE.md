# WP1 tenth migration slice: runtime policy adapter

**Status:** Focused verification passed

## Scope

Move the hot-reloadable runtime-policy implementation from the root module into
`sonder_runtime.adapters.runtime_policy`. The adapter owns policy-file location,
process-shared locking, deployment-transition reservation, revision-checked
updates, and route formatting; pure normalization remains in the domain rules.

## Completed work

- [x] Move the full production implementation under the package adapter boundary.
- [x] Rewire production callers, tests, package inventory, and nightly selfmod.
- [x] Retire the root module and shrink the legacy-root ratchet from 16 to 15.
- [x] Preserve cross-process lock and expected-revision behavior.
- [x] Extend the permanent retired-root reintroduction regression.

Focused verification: `264 passed, 2 skipped`; architecture, evidence, and
staged-diff checks pass.

The master-spec requirements remain unchecked; this slice is migration evidence,
not proof of a complete end-state requirement.
