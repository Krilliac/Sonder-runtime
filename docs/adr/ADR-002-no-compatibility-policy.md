# ADR-002: Final No-Compatibility Policy

**Status:** Accepted  
**Date:** 2026-08-09  
**Context:** SPEC-5 §M2, W1–W4

## Decision

Legacy compatibility is not an architectural objective. Legacy implementations, delegates, imports, environment bypasses, and duplicate execution paths are removed completely once their replacement slices are operational. No permanent compatibility shims, no permanent adapters/legacy, no root-level business-module delegates, no two production implementations of the same domain.

## Rationale

Sonder is designed for a single trusted operator, not as a public multi-tenant API. Internal Python import compatibility is unnecessary overhead. User data is preserved through explicit migration, not obsolete interfaces.

## Consequences

- adapters/legacy/ is deleted after all callers migrate
- Root business modules are deleted after extraction
- Bridge release provides one-time migration path
- Schema epoch 2 marks the clean break
