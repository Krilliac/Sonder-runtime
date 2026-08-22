# WP1 eleventh migration slice: Ollama endpoint adapter

**Status:** Focused verification passed

## Scope

Move endpoint normalization and fail-closed transport policy from the root
`ollama_endpoint.py` into `sonder_runtime.adapters.ollama.endpoint`. Rewire all
callers, package/selfmod inventories, and architecture reintroduction checks.

Focused verification: `93 passed, 2 skipped`; architecture, evidence, and
staged-diff checks pass.

The master-spec requirements remain unchecked; this slice is migration evidence,
not proof of a complete end-state requirement.
