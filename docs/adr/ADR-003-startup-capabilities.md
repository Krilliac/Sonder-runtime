# ADR-003: Startup Capabilities (--unrestricted-tools / --unrestricted-selfmod)

**Status:** Accepted  
**Date:** 2026-08-09  
**Context:** SPEC-5 §4, §17, §20

## Decision

Two independent frozen boolean capabilities are parsed at bootstrap and injected into the services that need them:

- `--unrestricted-tools`: disables model-tool authorization gates, enables HostCommandExecutor
- `--unrestricted-selfmod`: disables selfmod path/approval/isolation/test restrictions

Capabilities are immutable after startup. They cannot be toggled by HTTP, MCP, model output, agent, automation, or runtime configuration mutation. They are NOT placed in OperationContext.

## Rationale

Startup-only flags prevent privilege escalation through model output or API calls. Separating tool authority from selfmod authority gives operators fine-grained control. Not placing them in OperationContext prevents forgery.

## Consequences

- RuntimeCapabilities(frozen=True) added to bootstrap
- SONDER_UNSAFE_LAB_ACK environment path removed after migration
- Status endpoint reports capability state
- Reliability controls (deadlines, cancellation, logging) remain active in all modes
