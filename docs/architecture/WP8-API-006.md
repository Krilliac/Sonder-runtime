# WP8 API-006 — Operator control-plane snapshot

## Scope

`ControlPlaneSnapshot` is a typed, read-only presentation boundary for an
operator UI, diagnostics endpoint, or audit export. It exposes one immutable
section for sessions, plans, approvals, jobs, agents, model/hardware,
context, memory explanations, extensions, training, self-modification,
updates, health, and startup authorities.

## Contract

- `SnapshotSection` owns immutable record copies and reports a bounded count.
- `ControlPlaneSnapshot.build()` supplies empty sections for omitted domains.
- Frozen dataclasses prevent field reassignment; nested mappings and sequences
  are recursively frozen before publication.
- `as_dict()` is a stable transport representation and `digest()` gives a
  deterministic content identity for polling and audit comparisons.
- The snapshot is read-only: commands and mutation capabilities remain behind
  the owning application ports and are not exposed by this API.

## Verification

Focused coverage is in `tests/test_wp8_control_plane.py`. It verifies domain
exposure, recursive immutability, canonical digest stability, and validation
of malformed or unknown sections.
