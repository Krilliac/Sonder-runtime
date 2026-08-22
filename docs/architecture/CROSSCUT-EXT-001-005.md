# Cross-cutting extensions — EXT-001–005

The extension boundary is declarative and typed. `ExtensionManifest` carries
identity, semantic version, protocol, bounded dependencies, permissions,
health thresholds, and cleanup policy. It can adapt the existing plugin-lite
manifest without importing extension code.

`QuarantineRegistry` records deterministic admission decisions. Protocol,
dependency, and permission incompatibility quarantines an extension; repeated
crashes quarantine only after the manifest's bounded threshold. The registry
returns cleanup intent and state-retention policy but performs no process,
filesystem, or network mutation.

Evidence: `tests/test_crosscutting_extensions.py`, architecture/evidence gates,
compileall, and `git diff --check`.
