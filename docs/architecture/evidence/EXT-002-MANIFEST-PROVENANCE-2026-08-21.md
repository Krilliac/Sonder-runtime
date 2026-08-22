# EXT-002 manifest/provenance binding — 2026-08-21

## Scope

The extension admission boundary now binds provenance to the complete typed
`ExtensionManifest`, including identity, version, protocol, dependencies,
permissions, health thresholds, and cleanup policy. Admission fails closed with
an explicit `UNVERIFIED` health state when the provenance version or manifest
digest differs from the installed manifest. This prevents a signed record for
one extension revision from authorizing a changed manifest.

The digest is deterministic and computed in the domain object. The admission
service remains execution-neutral: it performs no extension import, process,
filesystem, or network operation. Existing signature verification, trust, and
quarantine policy boundaries remain unchanged.

## Evidence

- `sonder_runtime/domain/extensions/manifest.py`
- `sonder_runtime/application/extensions/provenance_inventory.py`
- `tests/test_remaining_extension_provenance.py`
- `python -m pytest -q tests/test_crosscutting_extensions.py tests/test_remaining_extension_provenance.py`
- `python -m compileall -q sonder_runtime/domain/extensions sonder_runtime/application/extensions`
- `git diff --check`

This closes the manifest-to-provenance validation slice of EXT-002. EXT-001 and
EXT-003–007 remain formally unverified until their respective integration and
operational evidence exists.
