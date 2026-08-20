# WP1 Sixty-Fourth Slice: HTTP Configuration Boundary

Status: implemented on `agent/wp1-execution-status`.

## Scope

The packaged HTTP interface now reads the API-key policy constant through
`sonder_runtime.platform.config`. The platform boundary re-exports the
canonical constant, while `sonder_config` remains the compatibility-backed
implementation. This preserves configuration object identity, defaults, and
environment parsing while removing the HTTP interface's direct root-module
caller.

## Evidence

- HTTP/config boundary and targeted HTTP bind-security regression tests:
  **11 passed**.
- The broader HTTP/config regression command exceeded its 124-second timeout;
  no failure result was returned.
- `python -m compileall -q sonder_runtime server.py`: passes.
- `scripts/check_architecture.py`: passes.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check` and `git diff --check`: pass.

## Remaining boundary

The platform module still re-exports the root configuration implementation;
this slice changes only the HTTP caller and does not claim that the broader
configuration migration is complete.
