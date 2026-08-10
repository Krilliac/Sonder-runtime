# ADR-005: Immutable Training Deployment Identity

**Status:** Accepted  
**Date:** 2026-08-09  
**Context:** SPEC-5 §25, M29, W13

## Decision

New trained model deployments use immutable identities `sonder-personal:<run-id>` instead of mutating `sonder-personal:latest`. Runtime policy owns active model selection via CAS revision. Rollback is a policy pointer change to a previous immutable identity, not alias mutation.

## Rationale

Mutable aliases make rollback ambiguous and training-cannot-activate-itself (M30) harder to enforce. Immutable identities provide deterministic deployment history and clean rollback semantics.

## Consequences

- Existing sonder-personal:latest migrated to sonder-personal:migrated-<digest-prefix>
- DeploymentService is sole runtime-policy mutator for model selection
- Training cannot promote itself — only DeploymentService after validation
- Final runtime refuses mutable personal alias as active canonical identity
