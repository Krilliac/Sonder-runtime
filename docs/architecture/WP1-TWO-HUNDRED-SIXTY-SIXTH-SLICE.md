# WP1 Two-Hundred-Sixty-Sixth Slice — fanout reporting error migration

## Boundary

Rewired the `model_fanout_status`, `model_fanout_recent`,
`model_fanout_cancel`, and `model_fanout_resume` endpoints to render their
validation and receipt failures through the packaged runtime model-error
adapter directly. The root formatter remains a compatibility delegate for
other callers.

## Evidence

- AST regression tests prove all four reporting endpoints contain no call to
  the root `_format_model_call_error` wrapper.
- Fanout, status, receipt, cancellation, resume, and server-helper regressions
  pass: **227 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Fanout synthesis and remaining agent/reporting paths still use the
compatibility delegate and remain staged for later migration. This slice does
not claim formal checklist completion.
