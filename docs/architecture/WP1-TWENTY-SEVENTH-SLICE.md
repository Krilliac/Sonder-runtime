# WP1 Twenty-Seventh Slice: Package the HTTP Serving Interface

Status: implemented on `agent/wp1-execution-status`.

## Scope

The OpenAI-compatible HTTP serving wrapper now lives at
`sonder_runtime.interfaces.http.serve`. The package CLI, tool-contract bridge,
serving tests, live-reload tests, auth/admission tests, and HTTP surface tests
use the package-qualified interface. Root `sonder_serve.py` is retired.

The interface remains a legacy-backed composition surface because it currently
binds the root server and several existing tool modules. The architecture gate
allows that dependency set only for this exact interface file; other interface
modules retain the normal strict dependency rules.

## Evidence

- Serving, lifecycle, admission, reload, auth, request-cache, body-framing,
  tool-contract, headless, and architecture regression: **90 passed** in the
  serving group, plus **48 architecture tests**.
- `scripts/check_architecture.py`: passes with five remaining legacy roots.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

Remaining roots are `server`, `sonder_migrations`, `sonder_repl`, and the two
immutable autopilot/fleet migration aliases. The server is the next major
composition-root boundary.
