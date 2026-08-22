# Remaining TOOL-006 / API-007 / API-008 — generated catalogs and mobile parity

## Implemented contract

`GeneratedCatalogs` remains the authoritative application projection of typed
tool descriptors, command inputs, and durable event schemas. It now emits
permission metadata and cross-surface conformance fixtures in addition to the
MCP, OpenAI, CLI, and client projections. `catalog_artifacts.py` renders the
six projections as deterministic JSON plus a SHA-256-bound manifest.

`scripts/generate_runtime_catalogs.py` accepts a plain JSON source for CI or a
composition root and supports both generation and `--check`; missing files,
changed files, and changed source contracts fail freshness checking. It uses
no SDK or network dependency.

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
  permission/conformance content, and missing/changed artifact gaps.
- `tests/test_remaining_client_schema.py` covers runtime schema freshness,
  bounded resume, continuation, and snapshot recovery.
- `tests/test_mobile_parity_wire.py` covers strict mobile request decoding,
  JSON-safe response/schema envelopes, continuation, and invalid cursors.
- Focused result: 17 passed.
- `check_architecture.py`, `check_requirement_evidence.py`, compileall, and
  `git diff --check` pass.

The master checklist and conservative requirement audit remain intentionally
unchanged; this document records implementation evidence only.
