# Archive-create canonical boundary evidence — 2026-08-21

The root-owned `archive_create.create_archive` capability is now reachable
through `sonder_runtime.application.ports.archive_create.ArchiveCreateGateway`
and `sonder_runtime.adapters.archive_create.ArchiveCreateAdapter`.

The adapter is intentionally thin and lazy-loads the root module. It forwards
all archive options, including caller limits and `developer_authorized`, to
the existing implementation. No safety policy was copied into the adapter:
the native implementation remains authoritative for workspace containment,
sensitive/control-state rejection, symlink/junction rejection, preflight and
revalidation, bounded file/entry/byte/depth/result limits, staging, atomic
publication, and non-overwrite behavior.

## Wiring status

The typed executor now constructs `ArchiveCreateRequest` and dispatches through
`ArchiveCreateAdapter`; the native MCP `archive_create` descriptor reaches that
executor path. Coverage is provided by
`tests/test_archive_create_boundary.py`,
`tests/test_archive_create_executor.py`, and the native catalog/transport tests.
The root implementation remains a compatibility backend until its internal
filesystem logic is fully packaged.
