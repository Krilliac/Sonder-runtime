# WP1 Two-Hundred-Sixtieth Slice — pure policy caller migration

## Boundary

Rewired canonical distillation, clean-retrieval, bounded-query, memory-tool,
agent-step, and model-inventory paths in `server.py` to invoke their packaged
policies directly. The root `_distillation_timeout_seconds`, `_no_retrieve`,
`_safe_limit`, and `_inventory_rows` helpers remain only as compatibility
delegates.

## Evidence

- Source-level regression tests prove production code contains no calls to
  those four compatibility wrappers.
- Distillation, learning, query-limit, inventory, retrieval, memory, history,
  and status regressions pass: **49 passed** in the focused slice.
- `git diff --check` and the architecture gate pass.

## Limitation

The compatibility delegates remain for existing external callers and tests.
Model error rendering, live cloud-tier refresh, and other root seams remain
staged; this slice does not claim formal checklist completion.
