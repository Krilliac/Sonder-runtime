# WP1 Two-Hundred-Fifty-Ninth Slice — model retry/error policy callers

## Boundary

Rewired model transport retry, embedded-response error, diagnostics, and
status paths in `server.py` to invoke the packaged retry and model-error
policies directly. The root `_local_model_retries`, `_local_retry_delay`, and
`_embedded_model_error` helpers remain only as compatibility delegates.

## Evidence

- Source-level regression tests prove production code contains no calls to
  those three compatibility wrappers.
- Local retry, embedded error, request-timeout, model-error, transport, and
  server regressions pass: **36 passed** in the focused slice.
- `git diff --check` and the architecture gate pass.

## Limitation

The root model-call error renderer and other compatibility delegates remain
staged for later migration. This slice does not claim full root-module removal
or formal checklist completion.
