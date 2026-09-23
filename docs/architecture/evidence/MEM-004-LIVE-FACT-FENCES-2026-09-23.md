# MEM-004: live fact fences and older-schema adoption

Status: implemented slice; the master MEM-004 checkbox remains open.

The live memory composition root (`compose_memory_unit_of_work`) activates one
configured source and project scope. After activation, no alternate fact
writer may change that scope outside the authoritative source:

- The legacy `memory_store.add_fact`/`delete_fact` helpers already refuse the
  activated scope.
- The replication projection now refuses to materialize, replace, or delete a
  fact in an activated scope. This covers both the fact receiver sink and
  projection rebuild. The refusal is raised inside the receiver transaction,
  so the receiver journal, projection log and cursor, fact rows, and derived
  indexes roll back together and no receipt is issued. A full-database dump
  taken before and after the rejected apply is identical.
- Projection into a scope that is not activated keeps its previous behavior.

Consequence: a node cannot currently own a scope authoritatively and also
accept peer fact batches for that same scope. Previously such a batch would
have produced unjournaled fact rows that made the next activation fail;
it is now rejected up front. Multi-writer reconciliation for one scope
remains unsupported.

Older-schema adoption is exercised end to end on a copy of a database created
with the original `facts` DDL and no authoritative tables: schema upgrade
through the normal memory store, a refused live start that publishes no
activation marker or journal row, operator dry run and backed-up apply, then
live `build_application` writes, a restart, and a journaled delete. Facts in
an unapproved project and the original file are left byte-for-byte unchanged.

Evidence: `tests/test_authoritative_live_fences.py`. With the projection
fence removed, the projection and receiver-sink tests fail (2 failed,
2 passed); with it restored all 4 pass.

Still open: no live user database has been migrated; automatic migration,
contradiction reconciliation, per-source multi-writer ownership, and broader
replay remain unverified under MEM-004 and issue #514.
