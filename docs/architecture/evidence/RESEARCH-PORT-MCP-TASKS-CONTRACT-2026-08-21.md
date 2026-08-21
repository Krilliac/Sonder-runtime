# Research port: MCP Tasks contract — 2026-08-21

## Decision

Sonder now has an application-level MCP Tasks-shaped projection over its
durable `JobRecord` identity. It provides a reconnectable `taskId`, revision,
status, timestamps, bounded polling advice, and explicit input-required state.
It does not add a background poller, transport route, or second job store.

## Mapping and boundaries

| Sonder job status | MCP Tasks status |
|---|---|
| pending, claimed, running, paused, interrupted | `working` |
| non-terminal job explicitly marked input-required by the adapter | `input_required` |
| succeeded | `completed` |
| failed | `failed` |
| cancelled | `cancelled` |

The projection reports only `resultAvailable` and `errorPresent`; it never
serializes result/error content. A future MCP transport can use the handle to
call the existing durable job service for authenticated polling, cancellation,
or a separately authorized result retrieval. Input submission is deliberately
not invented until a concrete durable input port exists.

## Evidence

- Contract: `sonder_runtime/application/protocol/mcp_tasks.py`
- Public protocol export: `sonder_runtime/application/protocol/__init__.py`
- Tests: `tests/test_mcp_tasks_projection.py`
- Existing durable owner: `sonder_runtime/application/ports/jobs.py`

Focused verification covers reconnect metadata, terminal mapping, explicit
input-required behavior, content non-disclosure, and bounds. This is a
contract/projection port, not a claim that an MCP Tasks transport or external
server interoperability test is complete.

Reference: <https://modelcontextprotocol.io/extensions/tasks/overview>
