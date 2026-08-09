# ADR-001: Inbound Interfaces Layer

**Status:** Accepted  
**Date:** 2026-08-09  
**Context:** SPEC-5 §28

## Decision

Create `sonder_runtime/interfaces/` with subdirectories `http/`, `mcp/`, `cli/`, `repl/` as the sole inbound protocol layer. Interfaces perform protocol translation, authentication, OperationContext creation, and application-service invocation. They do not implement business workflows, access databases directly, or instantiate infrastructure.

## Rationale

The current codebase has protocol handling interleaved with business logic (server.py is 18,102 LOC). Extracting a thin interfaces layer enforces the dependency rule: interfaces depend on application services, never on adapters or infrastructure directly.

## Consequences

- All HTTP/MCP/CLI/REPL entry points move under interfaces/
- server.py is decomposed and eventually deleted
- Architecture checker extended to reject interface→adapter imports
