# ADR namespace

New architecture decisions belong in this directory. Name each new record
`ADR-YYYY-MM-DD-<slug>.md`, using a real calendar date and a unique lowercase
slug. The documentation-authority check rejects new numeric IDs and new records
in `docs/architecture/adr/`.

The six existing `ADR-001` through `ADR-006` records here are the historical
SPEC-5 series. The nine numbered records in `docs/architecture/adr/` are the
historical architecture-program series. Both sets remain at their original
paths so old links and decision history survive; neither series assigns IDs to
new decisions. See [the historical series policy](../architecture/adr/README.md)
and [the authority map](../architecture/README.md) for their scope.

When a new ADR supersedes a historical decision, link that decision and state
the supersession explicitly. Do not reuse its numeric ID.
