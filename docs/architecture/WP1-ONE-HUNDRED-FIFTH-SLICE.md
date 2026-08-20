# WP1 One-Hundred-Fifth Slice — Service-State Ownership

## Scope

Move the complete `sonder_service_state` implementation into the packaged
platform boundary. Keep the root module as an identity-preserving compatibility
alias, and update packaged shutdown/lifecycle callers to use the canonical
module.

## Result

- `sonder_runtime.platform.service_state` owns `ProcessState`,
  `DependencyState`, `InvalidTransition`, snapshots, transition policy, and
  `ServiceStateTracker`.
- `sonder_service_state` contains only the compatibility alias, so legacy
  imports and monkeypatches address the same module object.
- `sonder_runtime.platform.shutdown` and
  `sonder_runtime.adapters.web.lifecycle` import service state from the
  canonical platform boundary.
- The `sonder_service_state` root allowance is removed from the architecture
  checker.

## Compatibility and safety evidence

The ownership tests prove module identity and public symbol identity. Existing
state-machine tests retain transition, dependency-readiness, listener, and
terminal-state behavior. Concurrent transition/readiness probes exercise the
lock-protected tracker while shutdown tests retain drain idempotence coverage.

## Verification

Run the focused state/lifecycle/shutdown tests, then:

```text
python -m compileall -q sonder_runtime server.py
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
git diff --cached --check
git diff --check
```
