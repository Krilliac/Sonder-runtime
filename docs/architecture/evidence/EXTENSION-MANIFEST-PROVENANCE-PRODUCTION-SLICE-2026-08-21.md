# Extension manifest/provenance production slice — 2026-08-21

## Scope

The existing extension boundary now has deterministic manifest identity and
dependency validation, order-stable manifest digests, provenance/trust
inventory exposure through the production registry health facade, and
fail-closed admission when the inventory is empty, untrusted, mismatched, or
tampered. SQLite rows validate all duplicated identity/version/digest fields
before they are admitted. Compatibility failures retain the existing explicit
quarantine decision and cleanup metadata.

This slice does not discover artifacts, launch extension code, or implement
cryptographic signature verification. Signature metadata is retained and any
verification hook remains explicitly injected; no production path claims a
signature is cryptographically verified.

## Evidence

- `sonder_runtime/domain/extensions/manifest.py`
- `sonder_runtime/application/extensions/provenance_inventory.py`
- `sonder_runtime/application/extensions/registry.py`
- `sonder_runtime/adapters/persistence/sqlite/extensions.py`
- `sonder_runtime/application/extensions/facade.py`
- `tests/test_crosscutting_extensions.py`
- `tests/test_remaining_extension_provenance.py`
- `tests/test_extension_production_slice.py`

Ledger revisions for EXT-001, EXT-002, and SEC-005 are conservatively marked
`implemented_unverified`.
