# Cross-cutting DATA-007 — immutable artifact manifests and attachment spill metadata

## Boundary

`sonder_runtime/application/artifacts/immutable_manifest.py` adds a
storage-neutral foundation over the existing `ArtifactHandle`, `AttachmentStore`,
and `SpillStore` ports. `ArtifactRecord` carries a complete SHA-256 digest,
size, media type, name, and explicit retention policy. `ArtifactManifestBuilder`
creates a deterministic full inventory and digest; `ImmutableReference` binds
an artifact digest and size to that manifest digest without exposing a path or
payload. `SpillMetadata` records the bounded spill ceiling and range-read
capability, while `bounded_range` rejects unbounded reads.

Retention is explicit through either a timezone-aware deadline or a bounded
read count. The module performs no filesystem, provider, network, or deletion
operation; concrete stores remain responsible for enforcing these metadata
contracts.

## Scope

This slice is additive and deliberately does not alter formal checklist
checkboxes, existing ports, persistence adapters, or composition wiring.

## Verification

```text
python -m pytest -q tests/test_crosscutting_artifacts.py
python -m compileall -q sonder_runtime/application/artifacts
python scripts/check_architecture.py
python scripts/check_requirement_evidence.py
git diff --check
```
