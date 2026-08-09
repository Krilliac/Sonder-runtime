# Queued-action lifecycle foundation

`queued_actions.py` is a local-only intent and state ledger. It stores immutable,
canonical action requests and append-only transitions through `proposed`,
`pending_approval`, `approved`, `executing`, and terminal states. Every change
uses an expected version and a revision-qualified SQLite update inside
`BEGIN IMMEDIATE`; transition IDs provide exact replay/idempotency behavior.

The trust boundary is intentionally stronger than the storage abstraction:

- a model may propose an inert request, but only `Actor.USER` can approve it;
- only `Actor.HOST` can enter execution or record completion/failure;
- exact enum actors are required, following the existing goal-store convention;
- scope is always local and payload/result/error fields are strictly bounded;
- SQLite triggers reject transition-history update or deletion.
- an executing user cancellation becomes `cancel_requested`; only the host may
  confirm `cancelled` after an execution checkpoint, while completion/failure
  remains available when an external side effect already finished;
- proposal intake stops at explicit total/open/history budgets. Existing actions
  may still reach terminal states, so intake pressure cannot strand cleanup.

This module deliberately has no executor, scheduler, background worker, MCP
tool, server import, permission bridge, network/cloud path, or autonomous state
advance. A payload grants no authority and must not contain secrets. Existing
goal/proposal stores remain responsible for objectives, command recovery remains
responsible for interrupted commands, and refinement transactions remain
responsible for reversible state improvements. Future execution adapters must
be separately reviewed and must re-check approval and host policy; this ledger
alone can never run an action.

The store is registered with Sonder's checksummed migration and backup inventory.
Append-only history has no ordinary delete path. When an intake ceiling is
reached, an operator must first bring every action to a terminal state, preserve
and verify the entire database as an archive, then rotate to a fresh migrated
store while the runtime is stopped. Partial deletion is not supported.

Actor enums are authorization claims supplied by a trusted future host adapter,
not identity authentication. No model-facing adapter exists in this phase. Any
future surface must derive the actor from authenticated host context rather than
accepting a model-provided string or enum.
