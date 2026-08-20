# WP1 twelfth migration slice: embedding cache adapter

**Status:** Focused verification passed

## Scope

Move the revision-pinned embedding cache from root `embed_cache.py` into
`sonder_runtime.adapters.embedding_cache`, rewire the embedding implementation
and cache tests, and remove root selfmod/package inventory entries.

Focused verification: `107 passed, 2 skipped`; architecture, evidence, and
staged-diff checks pass.

The master-spec requirements remain unchecked; this slice is migration evidence,
not proof of a complete end-state requirement.
