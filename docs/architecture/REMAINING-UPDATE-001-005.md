# UPDATE-001 / UPDATE-005 evidence

## Scope

This slice adds the missing bounded update domain and release-evidence
publication contracts without editing the formal master checklist or its
conservative audit.

### UPDATE-001 — bounded updates domain

`application/updates/bounded_state.py` provides one lifecycle coordinator for
download, digest verification, staging, health gating, activation, rollback,
and bounded history. Every transition is represented by an immutable
`UpdateSnapshot`; invalid order, bad artifact bytes, stale metadata, and failed
health/activation paths fail closed. The coordinator accepts ports and the
existing `AtomicReleaseActivator`, so it performs no network, subprocess,
pointer, or filesystem mutation.

`TufLikeMetadataChain` models the bounded root/timestamp/snapshot/targets
relationship. It validates immutable digest links, monotonic versions,
expiry, signer callbacks, and a target-count limit. It is a TUF-like
application contract; actual cryptographic signing remains an injected trust
adapter.

### UPDATE-005 — signed release evidence publication

`application/updates/publication.py` builds a deterministic in-memory bundle
from the existing `ReleaseEvidencePackage`. The bundle publishes a signed
manifest, `SHA256SUMS`, manifest bytes, SBOM, test results, and release
evidence containing migrations and rollback compatibility. Verification checks
the metadata chain, existing release signature/package digest, every target
digest, complete target set, and publication signature.

## Verification

`tests/test_remaining_update_001_005.py` covers:

- immutable metadata links, expiry, role ordering, and signature rejection;
- bounded lifecycle order, digest failure, health/activation, rollback, and
  history limits;
- deterministic signed publication with hashes, SBOM, tests, migrations, and
  rollback evidence; and
- target and publication-signature tampering.

All tests use injected in-memory ports and callbacks. No network or system
mutation is performed.
