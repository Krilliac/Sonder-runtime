# Optional PostgreSQL child storage

The durable child-session aggregate can use PostgreSQL instead of its existing
SQLite repository. SQLite remains the default. This changes the canonical child
rows, checkpoints, mutation intents and original operation receipts used by
delegation and lineage queries. It does not replicate other Sonder databases,
workspace files, interactive lane state or process execution.

Install the optional `requirements-postgres.txt` only in the runtime's intended
environment. The reviewed combination is PostgreSQL 18.6, psycopg 3.3.5 and
psycopg-pool 3.3.0. Other driver versions fail closed: pool worker termination
proof depends on the pinned pool's exact worker/scheduler structure. Upgrades
require the lifecycle and real database conformance tests again.

Explicit host configuration selects the backend:

```toml
[child_storage]
backend = "postgresql"
binding_file = "C:/private-sonder-storage/postgres-binding.json"
owner_id = "host-a"
durability = "sync-pair"
required_standby = "host_b"
pool_size = 2
operation_timeout_seconds = 5
cancel_timeout_seconds = 1
```

Equivalent environment fields use the prefix `SONDER_CHILD_STORAGE_`, followed
by the uppercase field name. An explicit PostgreSQL selection conflicts with
`SONDER_CHILD_SESSIONS_DB`; errors never fall back to SQLite. Invalid PostgreSQL
configuration or unavailable storage prevents application startup. Existing
SQLite installations need no PostgreSQL driver or binding.

The external binding is strict JSON, at most 16 KiB, with only `host`, `port`,
`database`, `user`, `passfile`, `sslmode` and optional `sslrootcert`. It contains
no arbitrary DSN, options, service selection or inline password. `passfile` and
`sslrootcert` are absolute paths in the same private directory as the binding.
The passfile is bounded to 16 KiB; the CA file to 1 MiB. Duplicate and unknown
keys fail validation. All ambient `PG*` environment variables are rejected.

Use an operator-owned directory outside every configured/model-writable root,
including the runtime home if writable. The private anchor rejects reparse
replacement, hard-linked files and broad file permissions. On Windows, file
access must be limited to the current user and SYSTEM; on POSIX, owner-only
access is required. Files and writable-root exclusions are revalidated at
connection and transaction boundaries. Do not place credentials in a repository,
prompt, transcript or runtime-writable directory.

Authentication is SCRAM with an explicit passfile. Only numeric loopback allows
`sslmode=disable`; remote hosts require `verify-full` and an explicit private CA
file. Client certificate/key authentication is not supported in this slice:
unknown certificate/key fields are rejected and `sslcertmode=disable` prevents
ambient client-certificate fallback. The binding cannot grant application or
workspace authority.

`primary` durability reports local acknowledgement. `sync-pair` requires the
database's exact `FIRST 1 (host_b)` policy (using the configured standby name)
and uses `remote_apply`. Durable logical receipts contain only the original
local outcome. A pair acknowledgement is an observation after an on-time,
warning-free COMMIT. Replaying a logical receipt must earn another pair
acknowledgement through a fresh WAL-bearing barrier under the child lock.
An old barrier row or lost response cannot prove a fresh acknowledgement.

The operation gate permits at most `pool_size` submitted/running SQL workers,
with no unbounded application queue. Each worker owns its connection through
both intent admission and state/receipt application. The external deadline
covers COMMIT; bounded cancellation and cleanup observation can add up to twice
`cancel_timeout_seconds`. Cancellation targets the exact leased connection.
Deadline, cancellation or a replication warning prevents another transaction
and prevents success, retry or new runner effects. Already committed effects
must be reconciled using the same immutable operation ID. Reconciliation is
refused until the original worker, cancellation and connection cleanup are
proved complete. Unresolved cleanup retains its occupied slot.

Same-child state changes lock a durable child lock row and independently verify
the earliest unreceipted intent in the state/receipt transaction. Original
receipts are retained; changed payloads under an existing operation ID conflict.
Capacity is 100,000 intents and 64 MiB of combined intent/result bytes, with
bounded per-record payloads inherited from the shared mutation port. Capacity
checks use aggregate scans under a metadata row lock. This is bounded storage,
not an indefinite-scale performance claim; no unresolved history is pruned.

One configured execution owner is admitted. A dedicated nonpooled session first
holds an aggregate-constant advisory lock, then commits its exact owner and
incarnation claim. Each storage operation verifies that backend identity and
durable incarnation. Losing the owner session permanently fences the incarnation;
it never reacquires the lock. An existing aggregate with missing owner metadata
is corruption, not a new namespace available for adoption.

Owned shutdown stops admission, joins delegated runners, proves active SQL and
cancellation cleanup, and joins the exact pool worker/scheduler handles against
one deadline. Only then may the still-locked owner session commit a clean marker.
Incomplete cleanup leaves owner admission unavailable. Native MCP closes these
resources only when explicitly owning its Application. HTTP and the default
REPL graph close their owned resources; default atexit cleanup never constructs
a new application. Closing the older compute-only API remains compute-only.

An unclean owner marker intentionally blocks replacement, including replacement
with the same owner ID. There is no automatic takeover, force-clean API, lease
expiry takeover or automatic failback. A reviewed recovery procedure requiring
independent old-owner cleanup proof remains required follow-up work. Do not edit
the marker manually to recover availability.

The staged offline migration/rollback utility is a separate required step.
Selecting PostgreSQL does not migrate an existing SQLite file. Do not cut over
existing child data until a verified current-state export/import and rollback
procedure is available. These database guarantees do not establish independent
host fencing or complete two-host Sonder high availability.

`tests/test_postgres_child_storage_integration.py` runs only with an explicit
disposable lab binding. Pair-loss cases require the in-process lab's trusted
standby controls. Tests do not accept arbitrary DSNs, reset a schema, install
services, or operate on an installed Sonder runtime.
