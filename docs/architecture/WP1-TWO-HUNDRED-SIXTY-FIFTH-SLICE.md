# WP1 Two-Hundred-Sixty-Fifth Slice — authorized fanout error migration

## Boundary

Rewired `_model_fanout_authorized`, the canonical fanout execution path, to
render configuration, provider, and missing-receipt failures through the
packaged runtime model-error adapter directly. The root formatter remains a
compatibility delegate for other callers.

## Evidence

- An AST regression test proves `_model_fanout_authorized` contains no call to
  the root `_format_model_call_error` wrapper.
- Model-fanout and model-retry regressions pass: **230 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Fanout status/resume/cancel endpoints and other agent/reporting paths still
use the compatibility delegate and remain staged for later migration. This
slice does not claim formal checklist completion.
