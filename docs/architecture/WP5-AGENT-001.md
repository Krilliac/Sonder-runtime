# WP5 AGENT-001/002 — Fleet and Autopilot registry adapter

## Boundary

`sonder_runtime.application.agent_registry.fleet_autopilot` is the bounded
translation seam for the existing Fleet and Autopilot entry points. Both modes
emit the same immutable `AgentLaunch` envelope and delegate creation,
resumption, cancellation, stopping, and status to one injected
`AgentRegistryPort`. The adapter performs input validation and metadata
normalization; it does not execute a mode-specific loop, open a store, or own
durability.

The envelope carries stable agent, operation, idempotency, and optional parent
identity. `AgentMode` retains the source mode as data for routing, telemetry,
and policy without creating a second execution contract.

## Evidence

- `tests/test_wp5_fleet_autopilot.py` verifies shared envelopes, mode and
  lineage preservation, immutable metadata, delegated lifecycle operations,
  and required identity validation.
- Focused gate: `python -m pytest tests/test_wp5_fleet_autopilot.py`.
- This slice adds no formal specification checkbox, commit, push, persistence
  migration, or changes to the legacy Fleet/Autopilot loops.
