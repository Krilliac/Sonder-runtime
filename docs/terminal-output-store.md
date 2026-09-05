# Private terminal output storage

Trusted host composition may inject `SQLiteTerminalOutputStore(private_directory,
model_writable_roots=live_inventory)` into its projection codec. The port accepts
the complete immutable `ProjectionBinding`, exact text, and a live authenticated
`OperationContext`. References contain content digest, UTF-8 size, and binding
digest; a reference is not authorization. No model or HTTP endpoint is provided.

The directory must be private and outside every configured and current-context
model-writable root, including containment in either direction. The inventory is
required and rechecked before each operation and before commit. The anchor checks
private directory identity and permissions. Database and SQLite journal/WAL/SHM
paths must be ordinary files with one link and no reparse points. Composition must
also keep this adapter and its trusted inventory inaccessible to model mutation.

One binding admits exactly one output. Identical retry validates stored bytes;
different output conflicts. UTF-8 is preserved without normalization. The default
per-output maximum is 1 MiB; aggregate quota is 64 MiB counting payload, canonical
binding bytes, and 192 bytes per reference/row, with at most 4096 rows. Operators
can lower limits. All quota decisions and insertion share BEGIN IMMEDIATE. There
is no eviction or deletion, including for unresolved data. SQLite page/index
overhead is separate from the logical quota: the main database is capped at 32768
pages (normally 128 MiB). Rollback journal storage is additional bounded transient
database overhead; this is not a claim of a 64 MiB physical disk ceiling.

Payload and binding share a FULL-synchronous SQLite commit before a put receipt
returns. A lost commit acknowledgement raises; identical retry reads the committed
row. Reads and retries check bounded stored lengths, exact binding, size, digest,
and valid UTF-8. The adapter retains no connections between operations. This is
single-host durable storage, not replication or protection against a malicious
process already acting as the same operating-system user. It does not add OS
filesystem or network isolation to child execution.
# Terminal projection integration

The host terminal codec keeps the existing schema-1 inline representation for
outputs up to 16 KiB. Larger outputs, up to the store's 1 MiB ceiling, use a
schema-2 envelope containing the exact content digest, UTF-8 byte count, and
complete projection-binding digest. The original text is committed before the
codec returns a projection. Restoration reads through a newly scoped live host
context, validates all digests, and never rewrites the blob. It does not truncate
text, normalize newlines, or convert an original failure into success.

The store and context provider must be injected by trusted host composition.
The reference itself grants no access. This codec integration is verified with
actual SQLite reopening; it does not by itself enable an app/REPL recovery route.
