# WP1 Two-Hundred-Sixty-Seventh Slice — synthesis and ensemble error migration

## Boundary

Rewired `model_fanout_synthesize` and `ensemble_answer` failure reporting to
use the packaged runtime model-error adapter directly. The root formatter
remains a compatibility delegate for remaining callers.

## Evidence

- AST regression tests prove both paths contain no call to the root
  `_format_model_call_error` wrapper.
- Fanout synthesis and ensemble regressions pass: **33 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Agent and remaining secondary reporting paths still use the compatibility
delegate; cloud-policy, MCP, and epoch-2 migration work remains. This slice
does not claim formal checklist completion.
