# Durable child storage migration

The offline migration module transfers the existing durable child aggregate between SQLite and PostgreSQL. SQLite remains the runtime default. This does not migrate conversations, agent lanes, artifacts, grants, or the rest of Sonder state, and does not provide failover or independent-host fencing.

Export, isolated staging, read-back verification, and same-ID copy resumption are operator commands. Installed service-manager activation is **unsupported**: the CLI cannot prove that old processes, database handles and launchers are stopped. There is no force flag. The host-internal disposable provider demonstrates cutover using an Application and private namespace it has owned since creation; it cannot adopt an installed namespace or reconstruct its authority after a process crash.

## Commands

Run `python -m sonder_runtime.bootstrap.child_migration --help`. Select each endpoint with `--source-sqlite PATH` / `--target-sqlite PATH` or `--source-postgres-binding PATH` / `--target-postgres-binding PATH`. PostgreSQL also requires `--owner-id ID`; use `--durability sync-pair --standby NAME` for the supported exact synchronous standby policy. The binding and its passfile/TLS closure retain ordinary live PostgreSQL private-file validation. Credentials are never accepted inline.

All commands require `--bundle PRIVATE_DIRECTORY`, outside every configured model-writable root. `export` returns a migration ID and counts. Supply that exact ID with `--migration-id ID` to `stage`, `resume`, `verify`, or `status`. `activate` returns a typed unsupported response before opening either store. Ordinary output contains no prompts, checkpoint bodies, private paths or raw driver diagnostics. Source/target selectors remain explicit on each invocation; sealed target identity conflicts fail closed.

Example, with an already configured private PostgreSQL binding:

```text
python -m sonder_runtime.bootstrap.child_migration export --source-sqlite ABSOLUTE_SOURCE_DB --target-postgres-binding ABSOLUTE_BINDING --owner-id OWNER --durability sync-pair --standby STANDBY --bundle ABSOLUTE_PRIVATE_BUNDLE
```

Use the same selectors and bundle for subsequent commands. Reverse migration requires a **new bundle and fresh export from PostgreSQL after its latest application writes**, targeting a new SQLite path. An old SQLite backup is not a rollback candidate after PostgreSQL has accepted writes.

Opening a PostgreSQL migration store obtains its dedicated aggregate advisory lock and validates the clean owner and exact replication policy. It may initialize an empty canonical namespace and its persistent migration identity. This is an offline administrative storage operation, not a read-only inventory endpoint; it cannot coexist with an active execution owner. No arbitrary SQL namespace or connection URL is accepted.

## Data and bounded recovery

Version 1 private bundles retain canonical child snapshots, sparse creation/intent positions, sequence high-water marks and original immutable intent/result bytes. Source owner metadata is provenance, never transferred ownership. SQLite exports also retain a supported database backup, its hash and original database/sidecar file identities. A retained plan fixes the migration ID before export. Completed backup/stream files can be reused after interruption; an unsealed, incompletely written SQLite backup is refused and retained for operator reconciliation rather than overwritten.

Limits are 100,000 records per stream, 64 MiB combined intent/result bytes, 512 MiB encoded streams, 3 MiB per line, and 100 records / 4 MiB per copy page. The optional physical SQLite backup has its own 512 MiB limit. Oversize or malformed history is rejected, never truncated. The manifest is at most 64 KiB. Pages and their exact digest receipts commit atomically. Target staging disables normal runtime admission; unresolved intents or active non-root children prevent staging/verification. Read-back covers every record and both sequence high-waters.

The PostgreSQL path uses the pinned optional driver and its existing bounded transport. Named cursors avoid fetching whole streams into memory. The external operation deadline covers SQL, encoding and output work; timeout does not seal a usable export or claim successful activation. Retry is unavailable until owned SQL/cancel/connection cleanup is proved. A fresh activation attempt emits fresh WAL and must complete its configured commit policy; a saved local phase is not reusable pair acknowledgement.

## Host cutover and remaining limits

The disposable host holds a cross-process launch lock for its lifetime, owns the exact factory-created Application repository, stops admissions, and proves tracked runner and SQLite/PG connection cleanup before issuing a live scoped guard. It compares current source contents, ordering and SQLite file/sidecar identity against the sealed export. Unknown existing handles and installed managers are not covered by this proof.

SQLite retirement preserves the source database under a new private filename and places a directory at its original pathname. This prevents future old-binary SQLite opens; it is not proof that an unknown existing handle was closed. PostgreSQL retirement retains the dedicated lock while recording retirement and an unclean retired-owner marker, which older PostgreSQL adapters also refuse. Neither marker is automatically cleared.

Activation records retirement intent, source retirement, target readiness, private host selection switch and completion as immutable phase entries. Same-ID retries in the still-live owning host retain source retirement after a lost target response. The process-local provider cannot resume activation after losing its from-inception ownership proof; a production service-manager provider with durable launch exclusion and authoritative cleanup remains required. Export and staged import remain usable without that provider.

The current host switches its own private selection record and validates the matching typed backend when constructing its next Application. It does not rewrite installed TOML, service definitions or live runtime configuration. Full installed migration, multi-host execution fencing, general unclean-owner recovery, and broader replicated Sonder state remain outstanding roadmap work.
# Migration authority identity and incomplete activation

PostgreSQL bundle and private selection identities bind the namespace, endpoint,
owner, durability, exact standby policy, operation/cancellation deadlines, and
an opaque digest of the anchored private binding closure (including TLS policy).
Changing that policy requires a fresh export; bundles from the earlier endpoint-only
identity implementation cannot be activated. Individual credential hashes are
never public status fields. Every migration authority admission revalidates the
live private closure. Starting the selected Application compares a freshly opened
binding against the retained policy before constructing its repository.

A PostgreSQL source must retain the exact exported owner incarnation and barrier,
even if no child rows changed. Retirement checks both again under the held aggregate
authority and row locks. A clean Application start/stop therefore requires a fresh
export before migration.

Any failure after cutover admission retains an incomplete-activation latch. The
disposable host refuses Application start and other migration IDs until the same
ID reconciles retirement, target readiness, its exact private selection marker,
and durable COMPLETE. Final selection is published only after COMPLETE. Unexpected
selection marker edits remain fenced. This latch belongs to the original live
host; it does not provide the still-missing installed service-manager crash recovery.
