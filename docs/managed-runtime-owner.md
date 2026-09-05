# Foreground managed runtime owner

This is an opt-in, host-only Windows composition for a required-new disposable
namespace. It does not adopt an installed runtime, expose an HTTP owner API,
install a service, or transfer authority after the foreground owner exits.
`ManagedRuntimeOwner(path, writable_roots=live_roots)` holds the namespace and
sibling workspace anchors and launch exclusion from creation through cleanup.
Existing paths are refused. The private path inventory is the exact owned root.

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
evidence requires all six component entries to be closed: child runner/storage,
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
