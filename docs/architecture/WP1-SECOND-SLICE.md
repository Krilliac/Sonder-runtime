# WP1 second migration slice: memory MMR adapter

**Status:** Implemented locally; CI verification pending
**Target requirements:** `ARCH-001`, `ARCH-002`, `ARCH-004`, `ARCH-010`, `MEM-006`
**Retired root module:** `mmr_rerank.py`

## Scope and rationale

The root module was a 55-line adapter around the already authoritative pure policy in
`sonder_runtime.domain.memory.rules.mmr_select`. It had one production caller, one
dedicated test module, no state, and no side effects beyond injected embedding codecs.
The migration moves the embedding-specific boundary to
`sonder_runtime.adapters.memory_rerank`, rewires its caller/tests, and deletes the root
module without a compatibility shim.

## Completed work

- [x] Move embedding defaults and blob decoding to the adapter layer.
- [x] Preserve `mmr_select` as the only domain implementation.
- [x] Rewire `retriever.py` and the dedicated tests.
- [x] Delete root `mmr_rerank.py`.
- [x] Add `mmr_rerank.py` to the permanent retired-root architecture ratchet.
- [x] Generalize the isolated reintroduction regression test across completed slices.
- [x] Update focused memory documentation.
- [ ] Mark master requirements verified. This slice alone does not prove any complete
  master requirement.

## Verification record

- [x] Dedicated context/MMR, architecture, evidence, and packaging selection: 138 passed.
- [x] Ruff passes for every newly introduced module and evidence test.
- [x] Architecture checker, requirement-evidence checker, and `git diff --check` pass.
- [x] Full suite reached 1,063 passed and 3 skipped before the first failure.
- [ ] Full-suite qualification in CI. This cloud sandbox reports spawned child PIDs that
  are not visible in its `/proc` namespace; the first failure is therefore the existing
  real-child liveness assertion in `tests/test_autopilot_store.py`. A direct subprocess
  probe reproduced the namespace mismatch independently of the migration.

## CI acceptance

The slice is accepted only when normal GitHub Actions PID visibility produces a green
full suite. Any non-environmental failure must be repaired before merge.
