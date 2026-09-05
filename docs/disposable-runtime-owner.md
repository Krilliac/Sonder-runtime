# Disposable runtime owner

This host-only Python composition implements the first two service-owner design
steps for a new workspace namespace. It is not an installed service manager,
authenticated external IPC endpoint, old-binary exclusion mechanism, or HA provider.
Existing launcher, HTTP, MCP and REPL defaults are unchanged.

`sonder_runtime.bootstrap.runtime_owner.DisposableRuntimeOwner(path,
writable_roots=live_roots)` requires a new private path and owns its launch lock
until `close()`. Its `private_source_paths` tuple contains the exact owned private
namespace for host admission inventory. No installed paths are guessed. The
child Application receives that same private provenance. A sibling disposable
workspace is the child's only configured model-writable root.

The host API has three prepared actions:

1. `prepare("select-id", "select", {"config": {"port": 12345}})` selects a fixed
   numeric-loopback HTTP port. Execute that immutable object using `execute()`.
2. `prepare("launch-id", "launch", {})` reserves the next launch before effects.
   `execute()` launches the fixed HTTP child through the existing process ledger.
3. `prepare("stop-id", "stop", {})` requests cooperative owned shutdown.
   `execute()` waits for Application closure, process exit, Job Object emptiness
   and the exact retained output-reader handles.

Retain the prepared object for retries. `execute()` with that exact identity
returns the original receipt; a different payload or revision under the same ID
is a conflict. Receipts describe original outcomes, not perpetual live health.
`status()` reports current durable state and explicit prototype scope.

The owner journal contains lifecycle operations/config generations only. It does
not replace child/task/process ledgers. SQLite transactions admit one expected
revision and one unresolved operation at a time, atomically retain original
receipts and immutable config selection, and preserve at most 1,024 operations.
Capacity exhaustion refuses new work without deleting history. Payloads/results
are bounded at 32 KiB before receipt envelope metadata.

On Windows, the child is created suspended and assigned to an owned Job Object
before resume. The actual base interpreter is launched directly, avoiding the
Windows virtualenv redirector's extra PID; dependencies still come from the
selected interpreter environment. The child checks its actual PID and creation
identity against the canonical job view and the pending launch before composing
the HTTP Application. Generated API credentials remain inside that child and
are neither returned to the caller nor stored in journal/config/output. `/live`
is available, while unauthenticated mutations remain denied.

Home/profile/appdata/temp/state paths are disposable. The model endpoint points
at the same disposable loopback listener, so this acceptance path does not contact
an installed model service or perform model work. Its purpose is lifecycle and
authority conformance, not a configured inference deployment.

Readiness and cleanup evidence are bounded private files bound to namespace,
job ID, PID/creation identity and selected config digest. These files are part of
same-user host composition; they do not authenticate a remote or untrusted local
IPC peer. A clean receipt requires successful child closure and native process
containment proof. Forced termination remains `STOPPED_UNCLEAN` and cannot enable
another launch. Output readers are retained at creation and checked under one
bounded join deadline; no global thread or process-name census supplies proof.

`execute(timeout=...)` accepts 1–30 seconds. Native termination/handle cleanup has
its own finite cleanup windows and can finish after that observation deadline;
late work cannot publish a new successful owner completion. Exact pending state
and any retained cleanup evidence remain available for same-ID reconciliation.
Unknown storage outcomes retain the exact command and suppress new effects.

Losing the owner process closes its Job handle, but reopening a journal only
reveals durable state; it does not recreate live authority. An existing namespace
cannot be adopted by constructing a new owner. Pending operations, RUNNING or
STOPPED_UNCLEAN state refuse new launch. Clean-process crash recovery, SCM/systemd
installation, authenticated IPC, PostgreSQL server-session reconciliation and
cross-host fencing remain separate work. There is no force-clear switch.
