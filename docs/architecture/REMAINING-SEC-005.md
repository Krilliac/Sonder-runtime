# REMAINING-SEC-005 — Extension provenance and SBOM inventory

## Scope

This slice closes the extension provenance/SBOM gap identified by the
requirement audit.  It is an application-boundary contract and performs no
extension import, network access, process launch, or filesystem mutation.

`provenance_inventory.py` records the extension source, artifact and manifest
digests, signature metadata, and an explicit trust record.  The signature
verifier is injected, so a record cannot be treated as verified merely because
it contains a signature string.  An untrusted or unverified record fails closed
before compatibility admission.

`SbomInventory` sorts entries by stable identity and derives a SHA-256 digest
from the complete canonical inventory.  Rebuilding from the same entries is
order-independent, while any changed component, version, source, license, or
artifact digest is detected as tampering.

`ExtensionProvenanceAdmission` composes these records with the existing
`QuarantineRegistry`.  Protocol, dependency, and permission incompatibility
therefore become an explicit quarantined health state, while valid verified
records become healthy.  No health state claims that an extension was executed
successfully; runtime probes remain a separate injected concern.

## Verification

`tests/test_remaining_extension_provenance.py` covers:

- retained source, signer, signature, and trust records;
- injected signature verification and fail-closed untrusted provenance;
- deterministic sorted SBOM construction and tamper detection; and
- compatibility failure mapped to quarantine plus health evidence.

Formal master-spec checkboxes remain intentionally unchanged.
