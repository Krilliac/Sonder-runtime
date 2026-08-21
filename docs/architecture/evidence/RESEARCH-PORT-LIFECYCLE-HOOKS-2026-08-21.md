# Research port: lifecycle hooks

Date: 2026-08-21  
Branch: `agent/port-research-findings`

## Finding

Zero and Easy Agent describe hooks as an extension point around the agent
loop, tools, and lifecycle. Sonder had several one-off callback seams, but no
shared bounded registry with deterministic ordering and observer isolation.

Sources: <https://github.com/gitlawb/zero> and
<https://github.com/ConardLi/easy-agent>

## Implemented slice

`LifecycleHookRegistry` now provides:

- bounded registration and payload sizes;
- deterministic priority/name dispatch;
- read-only payloads;
- failure isolation with typed failure summaries;
- explicit unregister and name introspection.

Hooks remain observers: a failing hook cannot turn the lifecycle operation into
a failed operation.

Evidence: `tests/test_lifecycle_hooks.py`.
