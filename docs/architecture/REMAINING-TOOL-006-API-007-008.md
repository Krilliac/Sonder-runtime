# Remaining TOOL-006 / API-007 / API-008 — generated catalogs and mobile parity

## Implemented contract

`GeneratedCatalogs` remains the authoritative application projection of typed
tool descriptors, command inputs, and durable event schemas. It now emits
permission metadata and cross-surface conformance fixtures in addition to the
MCP, OpenAI, CLI, and client projections. `catalog_artifacts.py` renders the
six projections as deterministic JSON plus a SHA-256-bound manifest.

`scripts/generate_runtime_catalogs.py` supports generation and `--check`
from either a plain JSON source (`--source`, for a mobile build without an SDK
or network dependency) or the live typed sources (`--runtime`: the native
tool registry, the slash-command catalog and `EventKind`). Missing files,
changed files, and changed source contracts fail freshness checking.

The `--runtime` projection is committed under
`docs/architecture/generated/runtime-catalogs/` by
`scripts/generate_documentation_catalogs.py --write`, and CI's
`check_documentation_authority.py` step fails when it is stale. Tools come
from the native registry because its descriptors carry effects and an
execution class; `permissions.json` lists exactly what each descriptor
declares, so a descriptor that declares no effects shows an empty list there.
The permission modes do not read this file; it is a published projection,
not the enforcement point.

`mobile_parity.py` defines a strict versioned JSON envelope for client schema
advertisement and reconnect requests/responses. It carries schema digests,
bounded stream cursors, continuation watermarks, snapshot state, event IDs,
and explicit freshness/disposition outcomes. Unknown fields, malformed
digests, invalid cursors, and unsupported versions fail closed. The contract
is transport/provider neutral and can be serialized by Flutter without
reimplementing stream semantics.

## Evidence

- `tests/test_remaining_tool_catalogs.py` covers deterministic projection and
  source digest behavior.
- `tests/test_tool_catalog_artifacts.py` covers all generated files,
  permission/conformance content, and missing/changed artifact gaps, and
  checks the committed runtime catalogs against the live registry (a drifted
  descriptor or a missing manifest makes `--runtime --check` exit 1).
- `tests/test_remaining_client_schema.py` covers runtime schema freshness,
  bounded resume, continuation, and snapshot recovery.
- `tests/test_mobile_parity_wire.py` covers strict mobile request decoding,
  JSON-safe response/schema envelopes, continuation, and invalid cursors.
- Focused result: 17 passed.
- `check_architecture.py`, `check_requirement_evidence.py`, compileall, and
  `git diff --check` pass.

The master checklist and conservative requirement audit remain intentionally
unchanged; this document records implementation evidence only.
