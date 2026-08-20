# WP1 thirteenth migration slice: embedding adapter

**Status:** Focused verification passed; NPU integration boundary remains

## Scope

Move the local embedding/vector implementation from root `embeddings.py` into
`sonder_runtime.adapters.embeddings`, rewire production and test callers, and
remove the root package/nightly inventory entry. Its endpoint and cache leaves
now live under package adapters as well.

The adapter still consumes the existing root `npu_service.py` integration
surface for optional accelerator routing. That dependency is explicit in the
architecture inventory and is the next migration boundary; it is not hidden or
claimed complete by this slice.

Focused verification: `470 passed, 7 skipped`; architecture, evidence, and
staged-diff checks pass.
