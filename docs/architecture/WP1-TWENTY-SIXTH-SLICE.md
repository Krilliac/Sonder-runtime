# WP1 Twenty-Sixth Slice: Package HTTP Lifecycle

Status: implemented on `agent/wp1-execution-status`.

## Scope

The lifecycle and admission implementation now lives at
`sonder_runtime.adapters.web.lifecycle`. Serving and lifecycle tests use the
package-qualified boundary, and root `sonder_lifecycle.py` is retired.

The existing `sonder_service_state` dependency is explicitly classified as a
platform-state boundary; no broad root import allowance was added.

## Evidence

- Admission fairness, lifecycle HTTP, served-action idempotency, and
  production architecture regression: **91 passed**.
- `scripts/check_architecture.py`: passes with the root legacy ratchet reduced
  to 7.
- `scripts/check_requirement_evidence.py`: passes.
- `git diff --cached --check`: passes.

## Remaining boundary

The remaining roots are server, immutable autopilot/fleet aliases, migration
registry, serving, and REPL. The lifecycle implementation is now fully inside
the package.
