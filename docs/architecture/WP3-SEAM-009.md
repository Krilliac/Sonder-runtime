# WP3 SEAM-009 — SubagentProvider

This document records the application port for local children and explicitly
configured external agents. It is provider-neutral and does not change the
existing fleet or workbench adapters.

## Contract

`sonder_runtime.application.ports.subagents` exposes `SubagentProvider`,
`SubagentHandle`, immutable request/snapshot/result envelopes, and budget
validation. A provider is the sole owner of child execution resources. A
handle is a non-owning capability and cannot transfer ownership.

Every child has an explicit `parent_id`; the provider must reject an unknown
parent and preserve that linkage in snapshots and the terminal result. The
provider assigns `child_id` when omitted. Child states are `created` → `queued`
→ `running` → one terminal state: `succeeded`, `failed`, `cancelled`, or
`timed_out`. Providers may skip intermediate states, but never publish a
terminal result as a live state or change a terminal result later.

## Budgets

`SubagentBudget` provides hard ceilings for child count, steps, wall time, and
output tokens. A request must carry at least one ceiling. A nested child may
only use equal or smaller finite parent limits; an omitted child limit cannot
mean “unlimited” when the parent has a finite limit. Providers must enforce
limits at the execution boundary and report exhaustion as `timed_out` or
`failed` with a structured `SubagentError`.

## Cancellation and cleanup

Cancellation is cooperative and idempotent; the first reason wins. Parent
cancellation propagates to every descendant, while cancelling one child does
not cancel its parent or siblings. `cancel` may return before execution stops.
`SubagentHandle.result()` joins one child, and `close()` is the provider-wide
cleanup boundary: it rejects new children, requests cancellation of live
children, and returns `True` only after all children are quiescent. Provider
cleanup must not leave a child running after it returns successfully.

## Result protocol

Every child ends with exactly one `SubagentResult`. Successful results contain
text output and no error. Failed, cancelled, and timed-out results contain a
structured error; cancellation is never represented as an empty successful
result. `SubagentUsage` carries measured steps, output tokens, and wall time,
with non-negative, finite values only. Unknown child IDs and malformed
provider envelopes are contract errors.

The port has no persistence, model routing, fleet scheduling, workbench
execution, or external-agent implementation. Those concerns belong to future
adapters that conform to this contract.
