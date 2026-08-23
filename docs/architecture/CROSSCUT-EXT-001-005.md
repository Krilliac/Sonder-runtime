# Cross-cutting extensions — EXT-001–005

The extension boundary is declarative and typed. `ExtensionManifest` carries
identity, semantic version, protocol, bounded dependencies, permissions,
health thresholds, and cleanup policy. It can adapt the existing plugin-lite
manifest without importing extension code.

`ExtensionManifest.to_dict()`/`from_dict()` are the one canonical, versioned
wire/persistence representation (`MANIFEST_SCHEMA_VERSION`). Every adapter
that needs to serialize a manifest — the SQLite state repository today, a
future MCP/HTTP capability-discovery surface tomorrow — goes through these
methods rather than hand-rolling the field shape, so they stay in lockstep as
the manifest gains fields. `from_dict` accepts a dict missing `schema_version`
(schema "1" by definition, predating the field) and ignores unknown extra
keys, so older and newer readers stay interoperable. `digest()` deliberately
excludes `schema_version`: it binds provenance to manifest *content*, so
evolving the wire shape never invalidates an already-signed provenance record.

`ExtensionApplicationFacade.preview_compatibility` exposes the manifest's own
`compatibility_reasons` check as a read-only preflight — "would this be
admitted?" — gated by its own narrow authority operation, with no artifact
fetch, no persistence write, and no broadened permission. It lets an install
wizard or an MCP capability-discovery tool validate a manifest before a real
`install` call, the same shape MCP's capability negotiation and PydanticAI's
validate-before-execute patterns use.

`QuarantineRegistry` records deterministic admission decisions. Protocol,
dependency, and permission incompatibility quarantines an extension; repeated
crashes quarantine only after the manifest's bounded threshold. The registry
returns cleanup intent and state-retention policy but performs no process,
filesystem, or network mutation.

MCP and HTTP structured error responses (`interfaces/mcp/handlers.error_result`,
`interfaces/http/handlers.error_response`) now carry the application error
taxonomy's `retryable` flag alongside `code`/`message`, so a caller can decide
to retry without pattern-matching the error code.

Evidence: `tests/test_crosscutting_extensions.py`,
`tests/test_extension_manifest_schema.py`, `tests/test_extension_facade.py`,
`tests/test_spec5_interfaces.py`, architecture/evidence gates, compileall, and
`git diff --check`.
