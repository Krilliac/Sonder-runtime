# Legacy sandbox API retirement

The internal `sonder_runtime.adapters.execution.sandbox` runner is retired.
Repository inspection found no production callers; `isolated_runner.py` and the
durable process provider are separate implementations and remain unchanged.

The old runner declared memory, network and path policies without enforcing
them, and silently executed container requests as host subprocesses. Its import
path, policy data and result types remain available for compatibility. Both
execution functions now return `LEGACY_SANDBOX_UNAVAILABLE`, with a failed result,
before starting a process or creating a script. This applies to every old level,
including `NONE`; there is no implicit host fallback.

Callers requiring container isolation should use the application-owned
`isolated_runner` service, which refuses execution when its configured runtime is
unavailable. Intentional host commands should use a properly authorized durable
`ProcessJobProvider` with explicit command, environment, workspace, capacity and
cleanup contracts. Process containment alone is not network or filesystem
isolation. The current application sandbox port describes the lifecycle contract;
the retained legacy value types are not evidence of an enforced boundary.

Tests that previously required the unsafe behavior have been replaced with
no-process/no-script refusal checks for every legacy level. Production container
and durable process behavior retain their dedicated regression suites. This is
an intentional compatibility behavior change, not a platform skip or a claim
that the old sandbox was repaired.
