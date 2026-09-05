# Foreground managed runtime owner

This is an opt-in, host-only Windows composition for a required-new disposable
namespace. It does not adopt an installed runtime, expose an HTTP owner API,
install a service, or transfer authority after the foreground owner exits.
`ManagedRuntimeOwner(path, writable_roots=live_roots)` holds the namespace and
sibling workspace anchors and launch exclusion from creation through cleanup.
Existing paths are refused. The private path inventory contains the exact owned root and any registered PostgreSQL credential bundle directories.

Managed launches use a required-new private copy of the Sonder Python package,
root compatibility modules, migrations and seed data. They never use the live
checkout as cwd or import path. A bounded canonical manifest covers this copy,
the resolved Windows CPython 3.12 executable/runtime files, standard library,
DLL directory and the current isolated environment's site-packages. Unknown
runtime path configurations, reparse entries, more than 50,000 files, more than
4 GiB of external/payload files, or a manifest larger than 32 MiB are refused.
The Sonder copy itself is limited to 10,000 files and 256 MiB.

The digest is bound into configuration, prepared launch, process metadata and
READY/CLEAN evidence. Content/identity and live writable-root separation are
rechecked before the process effect, immediately before native spawn, and at
child startup. The child uses explicit import paths with `-E -S`, no user-site
or `.pth` execution, a private pycache prefix with bytecode writes disabled,
and a minimal declared runtime DLL PATH. Standard operating-system libraries
remain part of the trusted Windows platform. Files belonging to another
trusted same-user host administrator are **not** write-locked for the duration
of execution: hashes and directory anchors do not prevent that administrator
from updating them after validation. Such concurrent host updates are outside
this slice's guarantee; model-writable closure paths are refused.

The owner stores prepared operations in schema 2 of the existing owner journal.
The immutable identity includes namespace, incarnation, epoch, owner revision,
configuration revision and child-selector revision. Configurations are bounded,
private content-addressed documents. Selecting a different child aggregate is
refused outside migration activation. Journal receipts retain the original
result, including after response loss; pending operations cannot be replaced by
a new ID. Schema 1 readers cannot accidentally admit schema 2 state.

The fixed child constructs one Application and installs it as the terminal
compatibility default before serving requests. It installs the reviewed SQLite
and thread factories before opening stores. HTTP requests use bounded owned
threads and exact socket tracking; the owned request and stream timeout profile
is five seconds. This is not a bound on arbitrary model execution duration.

Readiness evidence binds the actual process/job identity, namespace,
incarnation, epoch, selected configuration and fixed component manifest. Clean
evidence requires all seven component entries to be closed: app-work dispatcher, child runner/storage,
specialized providers and compute, owned workers, exact HTTP sockets, SQLite
handles, and the Application session repository. Provider closure uses the
concrete registry's typed cleanup/unregister checks, not a generic
`close_providers()` result. Workers and SQLite handles retain unresolved work;
Python threads are never declared terminated by cancellation. The enclosing
Windows owner additionally requires zero job accounting and every retained
process handle signaled before accepting process cleanup.

The same live owner issues child migration guards after that cleanup. It reuses
the existing activation algorithm and source checks, including retained phases,
source retirement and exact target selection. Incomplete activation blocks
launch and public selected-store access; only the exact prepared operation may
reconcile. The owner journal publishes the new configuration only after durable
bundle COMPLETE. Losing a stop receipt response can be reconciled without
retaining a stale live-process marker or authorizing a duplicate launch.

The child installs one fixed app-work slot before listener construction.
`register_owned_app_work(application, dispatcher)` accepts only the concrete
dispatcher bound intrinsically to that exact owned Application, with its pool
owned by the runtime's worker factory. Registration seals before listener
publication. `require_owned_app_work(application)` returns only that existing
registration; there is no lazy unowned fallback. The slot drains first during
shutdown. The dispatcher's concrete close routine proves local submission and
lifetime drain only; worker, SQLite and native process closure remain separate
requirements. A late or failed close preserves an unresolved original receipt.
The current fixed foreground configuration keeps app control disabled; an
installed empty slot does not advertise managed work availability or construct
an executor. Enabled app-work HTTP composition is a separate typed startup hook.

## Current limits and remaining work

The contained runtime profile admits owned SQLite and explicitly selected
PostgreSQL child storage through the migration protocol described below.
Other application aggregates retain their existing storage; selecting child
PostgreSQL does not establish all-aggregate replication or HA.

An owner crash leaves durable pending state and loses its live issuer. Reopening
the journal is observational; it does not grant recovery or clean a namespace.
Authenticated manager IPC, an installed service-manager provider, proven
cross-process restart reconciliation and all-aggregate eligibility remain to be
implemented. This prototype provides neither old-binary exclusion for unknown
writers nor independent-host fencing, automatic takeover, or HA. It must not be
used to advertise those capabilities. No installed data is changed by the tests.

App-work startup registration returns a private, exact slot lease. The constructor commits it only after current configuration and inventory checks; listener sealing refuses an incomplete lease. A failed constructor can roll back only its own uncommitted lease, after the concrete dispatcher drain finishes within the deadline. Failed or late cleanup retains the slot and prevents replacement.

Shutdown fences dispatch admission synchronously and cancels queued futures. One retained runtime-owned thread drains the concrete dispatcher. A deadline returns an unresolved component proof while that thread and dispatcher remain owned; later resource and OS containment cleanup can continue. Running Python callbacks are not terminated or reported clean merely because the deadline elapsed. Legacy caller-owned dispatcher close remains synchronous by default.

## Selected PostgreSQL child storage

The required-new foreground owner can register an exact `PostgresChildMigrationStore` target and its existing typed `ChildStorageConfig`. Activation still requires the reviewed export/stage/verification and live owner guard; selecting a configuration cannot bypass it. SQLite remains the default. A PostgreSQL launch requires the supported optional driver and its dependencies inside the declared interpreter environment; there is no SQLite fallback.

Configuration binds the exact policy and database namespace digest: endpoint/database/user, configured owner, durability/required standby, operation/cancel deadlines and private binding/TLS closure. The contained repository checks that digest under the aggregate owner lock before owner mutation and during subsequent transaction admission. Selected startup does not create missing schema or namespace metadata. Wrong identity, different owner, weaker policy and changed namespace refuse admission.

The parent closes its migration lock and pool before launching the child. After proven child cleanup, it can reopen only the exact selected policy/namespace. Retired and current migration handles remain explicitly owned; shutdown closes all retained handles under one bounded deadline. Store and private inventory retention are bounded. Binding paths remain private and must stay disjoint from live model-writable roots.

Reverse migration exports fresh current PostgreSQL state after cleanup, including any newer committed records; it never promotes the original SQLite backup. The disposable acceptance fixture verifies canonical data written after the PG runtime stops and closes its exact fixture repository before export. This fixture performs no model operations.

This remains a single-host foreground composition. It does not provide installed-manager adoption, owner recovery after a process crash, remote fencing, automatic takeover/failback, or replication eligibility for other application aggregates. A PostgreSQL pair acknowledgement is not proof that a previous runtime owner or remote machine has stopped.
