# WP1 Two-Hundred-Fifty-Fifth Slice — runtime-summary caller migration

## Boundary

Rewired the canonical `diagnostics()` and `status()` production paths in
`server.py` to invoke the packaged local-model option, context-selection, and
runtime-summary policies directly. The root `_local_runtime_summary` helper
remains only as a compatibility delegate for existing callers and tests.

## Evidence

- A source-level regression test proves that production code contains no call
  to the `_local_runtime_summary` compatibility wrapper.
- Runtime, environment, context-selection, HTTP, Ollama, server-helper, and
  model-fanout regressions pass: **510 passed**.
- `git diff --check` passes.

## Limitation

Other compatibility helpers in `server.py` remain intentionally staged for
later caller migrations; this slice does not claim full root-module removal or
formal checklist completion.
