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

Review follow-up: a rejected batch would be retried forever by its peer, so
the receiver now fails fast. The live application graph composes its
replication service with `authoritative_fact_scope_owned=True`, and
`receiver()` raises a `ConfigError` naming the scope before opening a
database; `serve.main` stops before the listener binds. A standalone service
refuses a receiver database that already has an activation marker for the
scope, and still serves one that does not. The HTTP lifecycle tests in
`tests/test_memory_replication_service.py` that only covered route lifecycle
now use `receiver_enabled=False`.

Retired coverage: `serve.main` coverage of attaching and detaching a receiver
for the live application graph was removed, because the live graph can no
longer produce a receiver. That path is unreachable from the live app, not
deleted. `configure_memory_replication_service` still installs and detaches a
receiver for a directly composed standalone service, which
`test_http_owner_composes_only_the_local_receiver_without_peer_send` continues
to cover, and `test_serve_main_refuses_receiver_before_listener_bind` covers
the live refusal. `docs/runbooks/memory-replication.md` now marks live two-PC
fact copy and its acceptance template as unavailable in this release.

## Per-transaction activation cost

Full per-row journal authentication now runs only when a source first claims
a scope (and in migration plan/apply). Re-entry on a scope already claimed by
the same source uses indexed anti-joins for ownership, exact versioned
upsert/delete journal presence, and live-row materialization. Payload bytes
are not re-authenticated on every transaction.

Measured locally on 2000 facts x 384-dim embeddings (per empty unit of work,
minimum of 5): 0.614 s before the change versus a 0.0096 s reference query
(the anti-join main runs before this PR); after the change the bounded test
passes. A 3000 x 384 run after the change measured 0.039 s per empty unit of
work against a 0.014 s reference query. Evidence:
`tests/test_authoritative_activation_cost.py` (call-count test and a timing
bound of `max(0.25 s, 20 x reference)`, both failing before the change) plus
corruption cases showing re-entry still fails closed on a dropped journal
row, a dropped fact row, or a mismatched state version.

Older-schema adoption is exercised end to end on a copy of a database created
with the original `facts` DDL and no authoritative tables: schema upgrade
through the normal memory store, a refused live start that publishes no
activation marker or journal row, operator dry run and backed-up apply, then
live `build_application` writes, a restart, and a journaled delete. Facts in
an unapproved project and the original file are left byte-for-byte unchanged.

Evidence: `tests/test_authoritative_live_fences.py`. With the projection
fence removed, the projection and receiver-sink tests fail (2 failed,
2 passed); with the receiver refusal removed, the three receiver-startup
tests fail (3 failed, 4 passed); with both in place all 7 pass.

Still open: no live user database has been migrated; automatic migration,
contradiction reconciliation, per-source multi-writer ownership, and broader
replay remain unverified under MEM-004 and issue #514.
