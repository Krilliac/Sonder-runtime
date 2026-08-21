# WP1 text-patch ownership consolidation evidence

Date: 2026-08-21

## Scope

Retired the root `text_patch.py` compatibility path. The canonical
`sonder_runtime/adapters/filesystem/text_patch.py` implementation is now the
only text-patch module, and the server imports it directly. Native MCP and
selfmod files were outside this change.

## Ownership and compatibility decision

The packaged filesystem adapter is the sole implementation and ownership
boundary. Production code no longer imports the root name, and the root module
is absent. Existing bounded parsing, path/sensitive-file checks, UTF-8 and
binary rejection, transactional staging, revalidation, publication, and
rollback semantics remain in the packaged adapter.

## Focused evidence

`tests/test_text_patch_compatibility.py` verifies root absence, direct server
ownership, and the packaged safety-sensitive API. `tests/test_text_patch.py`
exercises the full preview/apply, path-safety, cap, and transaction/rollback
behavior through the packaged adapter.

## Verification

Command:

`python -m pytest -q --basetemp .pytest-wp1-text-patch tests/test_text_patch.py tests/test_text_patch_compatibility.py`

Result: to be refreshed after the bounded retirement change. The test set
covers the same safety behavior plus root absence and direct packaged
ownership.
