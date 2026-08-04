# ADR-005: Explicit operation context

**Status:** accepted

## Context

Principal, privilege, deadlines, cancellation, workspace roots, and
consent were implicit (globals, environment, transport state).

## Decision

`sonder_runtime.application.context.OperationContext` — frozen, request
scoped — travels through every privileged entry point. A single audited
factory (`local_owner_context`) builds the single-owner context legacy
REPL calls imply. `principal_id` defaults to the one owner: the identity
seam exists (SPEC-3 R-M14) without any multi-user implementation.

## Consequences

New application interfaces take context explicitly; the HTTP adapter
builds it from its admission layer (correlation ID, auth level,
deadline, cancellation).
