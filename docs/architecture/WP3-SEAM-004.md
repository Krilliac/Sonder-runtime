# WP3 SEAM-004 — Shared execution world

This slice defines the application port for `SubprocessRuntime`,
`ShellExecutor`, and `TerminalService`. It does not alter existing adapters or
move any call site.

## Contract

- One `ExecutionWorld` is the lifecycle owner for subprocesses, one-shot shell
  executions, and persistent terminals. All three services are obtained from
  that world, so a filesystem/shell/subprocess/terminal adapter can later bind
  them to the same local, container, or remote environment.
- A returned `SubprocessHandle` or `TerminalHandle` is a non-owning capability.
  Handle `close()` releases its resource and is idempotent; it never closes the
  world. The world remains responsible for cleanup if a caller loses a handle.
- `OperationContext.cancellation` is passed to every start/execute/open call.
  Adapters must observe it and stop their resource. World `cancel()` is
  idempotent, first-reason-wins, and rejects new work after the request.
- Cancellation is not quiescence. `cleanup(timeout)` is the shutdown barrier:
  it rejects new work, propagates cancellation to all children, releases
  adapter resources, and waits for every owned resource to exit. A successful
  `CleanupResult.quiescent` means `active_resources == 0`; a timeout reports
  incomplete cleanup without claiming quiescence.
- Cleanup is repeatable. An adapter must permit a later cleanup call to finish
  shutdown after an earlier bounded call returned incomplete.

## Ownership and ordering

```
ExecutionWorld (sole owner)
├── SubprocessRuntime → SubprocessHandle (borrowed capability)
├── ShellExecutor     → one-shot lease (world-owned)
└── TerminalService   → TerminalHandle (borrowed capability)
```

Shutdown ordering is: stop admission → request cancellation → terminate/close
children → join workers and drain terminal I/O → report quiescence. No adapter
may expose a child after the world has reached `CLOSED`, and no caller may use
a handle as evidence that the whole world is quiescent.

## Boundaries

The application port contains no `subprocess`, shell, terminal, filesystem, or
environment implementation. Concrete adapters remain unchanged and are the
only place where platform process APIs belong. Security isolation is not
implied by this contract; a sandbox provider is a separate seam.

## Verification

`tests/test_execution_world_port.py` uses protocol-shaped fakes to verify the
shared-world identity, non-owning handles, cancellation propagation, and the
cancellation-versus-quiescence distinction without starting a real process.
