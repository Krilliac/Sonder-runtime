# WP3 SEAM-005 — SandboxProvider

## Boundary

`sonder_runtime/application/ports/sandbox.py` defines the provider-neutral
application port for local, container, remote, and read-only execution worlds.
It is additive: no adapter, `unsafe_lab`, composition root, or existing
execution-world implementation is changed.

## Contract

- `SandboxWorldSpec` names the world kind and carries an immutable
  `SandboxPolicy`. The four kinds are explicit; `read_only` additionally
  requires a policy that forbids writes and persistent changes.
- Policy values are constraints, not grants. Providers may tighten them but
  must never widen write, network, process, egress, or persistence authority.
  The application contract validates contradictory requests before a provider
  receives them.
- `SandboxProvider.provision()` returns one `SandboxWorld` lifecycle owner.
  Its `execution_world` is the shared capability consumed by the SEAM-004
  filesystem/process/shell/terminal adapters, so those resources cannot be
  silently split across worlds.
- `cancel()` is idempotent cancellation intent and does not prove cleanup.
  `cleanup(timeout)` rejects new work, propagates cancellation, releases
  provider resources, and waits for quiescence. A timeout returns
  `quiescent=False` with the remaining resource count; cleanup may be retried.
- A provider owns isolation details, credentials, container/remote handles,
  and resource teardown. The port does not claim that a local world is a
  security boundary; adapters and receipts must report their actual isolation.

## Ownership

```text
SandboxProvider
└── SandboxWorld (sole lifecycle owner)
    └── ExecutionWorld (shared execution capability)
```

## Verification

`tests/test_sandbox_provider_port.py` covers the four world kinds, immutable
policy invariants, read-only fail-closed validation, provider shape, shared
world ownership, and cleanup/quiescence evidence without starting a process or
contacting a container or remote host.

Focused gates:

```text
python -m pytest -q tests/test_sandbox_provider_port.py
python -m compileall -q sonder_runtime/application/ports/sandbox.py
```
