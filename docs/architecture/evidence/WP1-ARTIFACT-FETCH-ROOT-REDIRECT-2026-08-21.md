# WP1 artifact-fetch root retirement evidence

## Closure item

The root `artifact_fetch.py` redirect has been retired. The packaged
`sonder_runtime.adapters.artifact_fetch` adapter is now the sole owner of
artifact download, verification, provenance, and formatting behavior.

## Verification

- `tests/test_artifact_fetch_compatibility.py` verifies that the root module is
  absent and that representative public API objects are owned by the canonical
  package.
- The existing `tests/test_artifact_fetch.py` suite imports and exercises the
  packaged adapter directly.
- `server.py` imports the packaged adapter directly; no package-internal import
  of the root module remains.
- `scripts/check_architecture.py` permanently ratchets `artifact_fetch.py` as
  a retired root module.

## Write set

The closure changes are the root-module deletion, direct production/test
imports, the focused ownership test, the architecture retirement ratchet, and
this evidence document.
