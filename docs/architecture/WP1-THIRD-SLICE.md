# WP1 third migration slice: reward facade retirement

**Status:** Implemented locally; CI verification pending
**Target requirements:** `ARCH-001`, `ARCH-002`, `ARCH-004`, `ARCH-010`, `MEM-005`
**Retired root module:** `reward.py`

## Scope and rationale

The root module contained no independent policy. It re-exported constants and renamed
functions from `sonder_runtime.domain.memory.rules`, leaving two public names for the
same reward classification and evidence-ordering behavior. This slice rewires every
production, proposal, test, and self-modification consumer to the domain rules and then
deletes the facade without a compatibility shim.

## Completed work

- [x] Rewire reward scoring to `reward_rules.reward_score`.
- [x] Rewire eligibility to `reward_rules.reward_is_good`.
- [x] Use explicit `OUTCOME_SOURCE_*` provenance constants.
- [x] Preserve the existing signal set, thresholds, population, and evidence ranking.
- [x] Repair the indirect `sonder_serve → server.reward` dependency.
- [x] Point nightly self-modification at the authoritative domain module.
- [x] Delete root `reward.py`.
- [x] Add `reward.py` to the retired-root architecture ratchet.
- [x] Update focused packaging and memory documentation.
- [ ] Mark master requirements verified. This slice proves a bounded migration, not a
  complete master requirement.

## Verification record

- [x] Static compilation of every directly changed Python consumer.
- [x] Architecture checker and requirement-evidence checker pass.
- [x] Source scan finds no production import of root `reward`.
- [x] Reward, memory-domain, export, learning-health, grounded-outcome, offload,
  repository, serve, architecture, and packaging selection: 470 passed.
- [ ] Full suite and cross-platform bundles in GitHub Actions.

## CI acceptance

Merge only after the normal CI and application-build workflows pass. Preserve the
master requirements as `planned` until broader ownership requirements have complete
evidence.
