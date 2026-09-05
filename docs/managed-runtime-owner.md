# Foreground managed runtime owner

This is an opt-in, host-only Windows composition for a required-new disposable
namespace. It does not adopt an installed runtime, expose an HTTP owner API,
install a service, or transfer authority after the foreground owner exits.
`ManagedRuntimeOwner(path, writable_roots=live_roots)` holds the namespace and
sibling workspace anchors and launch exclusion from creation through cleanup.
Existing paths are refused. The private path inventory is the exact owned root.

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

The contained runtime profile currently admits owned SQLite only. Existing
PostgreSQL export/stage/activation infrastructure is not yet wired into this
foreground runtime's startup policy/namespace admission. PostgreSQL runtime
selection is explicitly refused; there is no fallback to SQLite. The tested
composed migration is SQLite to a new SQLite target in the same owned namespace.

An owner crash leaves durable pending state and loses its live issuer. Reopening
the journal is observational; it does not grant recovery or clean a namespace.
Authenticated manager IPC, an installed service-manager provider, proven
cross-process restart reconciliation and all-aggregate eligibility remain to be
implemented. This prototype provides neither old-binary exclusion for unknown
writers nor independent-host fencing, automatic takeover, or HA. It must not be
used to advertise those capabilities. No installed data is changed by the tests.

App-work startup registration returns a private, exact slot lease. The constructor commits it only after current configuration and inventory checks; listener sealing refuses an incomplete lease. A failed constructor can roll back only its own uncommitted lease, after the concrete dispatcher drain finishes within the deadline. Failed or late cleanup retains the slot and prevents replacement.

Shutdown fences dispatch admission synchronously and cancels queued futures. One retained runtime-owned thread drains the concrete dispatcher. A deadline returns an unresolved component proof while that thread and dispatcher remain owned; later resource and OS containment cleanup can continue. Running Python callbacks are not terminated or reported clean merely because the deadline elapsed. Legacy caller-owned dispatcher close remains synchronous by default.
