# WP1 One-Hundred-Twelfth Slice — move unsafe-lab state behind the security adapter

## Boundary moved

The stateful unsafe-lab implementation now lives in
`sonder_runtime.adapters.security.unsafe_lab`. The root `unsafe_lab.py` module
continues to alias that module, preserving the public `active`,
`is_privileged`, `inspect`, `require_startup`, `status_line`, and
`_audited_processes` monkeypatch surfaces used by legacy callers and HTTP
startup.

The pure, explicit-input validation policy lives in
`sonder_runtime.platform.unsafe_lab_policy`. Platform configuration therefore
validates its merged environment without importing upward into application
security, and the application layer no longer owns host environment or audit
state.

## Security invariants

- Exact acknowledgement, loopback listener, local Ollama, cloud opt-in, and
  elevated-process refusals remain fail-closed.
- Activation audit writes remain process-deduplicated, flushed, fsynced, and
  permission-restricted on POSIX.
- HTTP startup still runs `require_startup(host=host)` before binding.
- The architecture checker remains unchanged as a constraint: no new
  exception or allowance was added.

## Verification

- `python scripts/check_architecture.py`: **PASS** (zero violations).
- Focused security, headless, entrypoint, and architecture suites:
  **176 passed**, 1 non-fatal pytest cache warning.
- The new ownership regression verifies the root alias resolves to the
  adapter implementation and that the former application implementation is
  absent.
