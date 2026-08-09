# ADR-006: Schema Epoch 2

**Status:** Accepted  
**Date:** 2026-08-09  
**Context:** SPEC-5 §34

## Decision

A bridge release migrates all state from SPEC-3 schema ownership to SPEC-5 domain ownership and stamps `schema_epoch = 2`. The final SPEC-5 runtime requires epoch 2; pre-epoch state fails with MigrationRequired. The bridge release handles backup, migration, adoption receipts, integrity verification, and epoch marking.

## Rationale

Completely removing legacy imports requires an explicit migration boundary. The bridge release is the single point where old state is adopted into new ownership. After epoch 2, the runtime carries no legacy migration code — old migration sources are archived for audit only.

## Consequences

- tasks move from memory.db to automation.db
- autopilot/fleet state adopted into automation.db
- Each domain gets independent migration ledger
- Final runtime: epoch 2 → start; no epoch → MigrationRequired
- ROOT_LEGACY_MODULES reaches 0 without abandoning user data
