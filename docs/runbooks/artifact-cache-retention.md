# Artifact cache retention runbook

This runbook describes how a host can consume the pure P5 artifact-retention
plan. The current runtime does not wire a destructive cache adapter to this
policy; the steps below are the required boundary for a future integration.

## Build a consistent snapshot

Collect three bounded, revisioned pages from the cache and its durable
registries:

- Cache entries contain the exact `(artifact_id, digest, version)` identity,
  byte size, last-access time, optional retention deadline, and cache revision.
- Owner references contain the exact identity, owner kind (`job` or
  `deployment`), owner state, reference revision, and an optional lease
  expiry. A released reference is explicit; a missing or unknown state is not
  proof that the bytes are unused.
- Tombstones contain the exact identity, kind (`retention` or `deletion`),
  revision, creation time, and bounded reason.

Use `ArtifactCachePage`, `ArtifactReferencePage`, and
`ArtifactTombstonePage` to mark whether each scan is complete and to carry its
revision/cursor. Never claim a page is complete after a bounded query stopped
at its limit. If a source cannot produce a complete page, pass `complete=False`;
the policy will defer cleanup.

```python
from datetime import datetime, timezone

from sonder_runtime.domain.artifact_retention import plan_artifact_gc

plan = plan_artifact_gc(
    cache_page,
    reference_page,
    tombstone_page,
    now=datetime.now(timezone.utc),
)
```

Inspect every decision. `retain` and `defer` are terminal outcomes for that
pass. A `delete` decision includes a matching deletion tombstone and may be
applied only while `plan.candidate_revision`, `plan.reference_revision`, and
`plan.tombstone_revision` still describe the same durable snapshots.

## Apply a deletion safely

1. Re-read or compare the three source revisions immediately before the side
   effect. Discard the plan if any revision changed.
2. Persist the emitted deletion tombstone before deleting the content-addressed
   bytes. The tombstone must use the exact artifact ID, digest, and version.
3. Delete only the adapter-owned object selected by that exact identity. Never
   derive a path from an artifact name, model output, or an unverified digest.
4. If deletion fails, retain the tombstone and retry from a fresh or exact
   idempotent plan. A later scan sees the marker and reasserts deletion instead
   of resurrecting the entry.
5. Record the decision reason and source revisions without recording payloads,
   prompts, credentials, or private paths.

A retention tombstone remains a durable hold. Remove one only through an
explicit, authorized lifecycle operation that records a new source revision;
then obtain a fresh complete plan. Do not interpret a lease timeout, a missing
row, or a truncated page as a release.

## Bounded operation

The default pass scans at most 256 candidates, 1,024 references, and 1,024
tombstones. Use `next_cursor` to continue candidate inventory pages. A
reference or tombstone page over its configured bound is incomplete and causes
all decisions in that pass to defer. Keep the policy pure and perform paging,
revision checks, persistence, and deletion in the adapter.

## Known limits

The policy does not discover job/deployment references, replicate artifacts,
coordinate nodes, provide failover, or guarantee physical deletion. It cannot
validate an external registry's honesty or survive a revision change after an
adapter's final check. Those responsibilities require a separately reviewed,
permissioned persistence and coordination integration.
