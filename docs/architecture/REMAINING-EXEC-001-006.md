# Remaining execution world and job-control slice

This slice closes the application-level contract gap between the existing
execution-world, sandbox, and generic-job ports. It is intentionally isolated:
it adds no provider, subprocess, filesystem, network, or container adapter and
does not edit the formal implementation checklist.

## EXEC-001 — Shared execution world

`SharedExecutionWorld` gives filesystem, shell, subprocess, terminal, LSP, and
code surfaces one stable world id and typed `WorldBinding` values. Consumers
can reject cross-world composition with `require_same_world`; the binding is a
capability reference, not an ownership claim.

## EXEC-002/005 — Provider-neutral world kinds

The world records whether its provider is local, container, or remote. The
contract does not silently grant container security or remote authority:
providers must bind the actual surfaces and carry their policy evidence.

## EXEC-003 — Terminal and job control

`ExecutionWorldController` groups start/list/poll/stream/cancel/collect with
open/reconnect/send/resize/stop terminal operations. The in-memory adapter is
only a deterministic contract fixture; production process and terminal
supervision remains in adapters.

## EXEC-004/005 — Bounded output and resumable cursors

`BoundedOutputBuffer` returns finite pages with monotonic `OutputWatermark`
cursors, explicit `has_more`, and `truncated` evidence when retention has
expired. Large-output adapters can attach a digest-bound `SpillReference`
instead of repeating a payload. A cursor never implies that omitted output was
successfully collected.

## EXEC-006 — Isolation truth labels

`IsolationTruth.FAILURE_ISOLATION_ONLY` means only that a provider can describe
failure containment. `SECURITY_BOUNDARY_VERIFIED` is reserved for a claim with
an explicit evidence reference. `UNVERIFIED` is the safe default. These labels
must be copied into execution receipts and operator presentation; no local
exception boundary, worktree, or process cleanup may be presented as a real
security boundary.

## Evidence

- `tests/test_remaining_execution_world.py` covers world identity, mismatch
  rejection, isolation truth, job/terminal lifecycle, bounded output, and
  cursor expiry.
- The module has no infrastructure imports and is validated by the architecture
  and requirement-evidence gates.
- Formal checklist checkboxes remain untouched by this slice.
