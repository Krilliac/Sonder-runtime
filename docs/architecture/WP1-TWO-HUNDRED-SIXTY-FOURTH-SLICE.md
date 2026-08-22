# WP1 Two-Hundred-Sixty-Fourth Slice — status and vision error migration

## Boundary

Rewired the user-visible `status()` and guarded `vision_analyze()` error
paths to invoke the packaged runtime model-error adapter directly. The root
`_format_model_call_error` helper remains as a compatibility delegate.

## Evidence

- AST regression tests prove both paths contain no call to the root formatter.
- Model-error, vision, status, diagnostics, and Ollama regressions pass:
  **39 passed**.
- `git diff --check` and the architecture gate pass.

## Limitation

Agent and fanout receipt/reporting paths still use the compatibility delegate
and remain staged for later migration. This slice does not claim formal
checklist completion.
