# Shared private control-plane paths

`adapters.security.control_plane_paths.live_control_plane_inventory()` resolves
current host paths without creating directories, opening databases or invoking
memory migration. It follows the existing state-path precedence: configured
process home overrides state_path environment values; canonical memory uses
SONDER_DB; canonical sessions explicitly prefers SONDER_SESSIONS_DB as app.py
does. The app task fallback STATE_HOME/memory.db is also protected. The existing
execution spill resolver uses SONDER_JOBS_DB, so that exact override is shared
with jobs; this module does not invent a spill environment setting.

The default inventory covers fleet/approvals/memory/sessions/jobs/spill/child
sessions/fanout/extensions databases and all SQLite WAL, SHM and rollback-journal
sidecars, fleet-principal credentials, adjacent lane-owner lock names, configured
verifier catalog, and tool audit plus its rotated filenames. File_ops retains its
existing configuration/secret/runtime-code protection and adds this live inventory.
Ordinary sibling files are not classified as private solely because their parent
contains a database. Existing authenticated developer override semantics remain.

For constructor-injected stores use immutable `ControlPlanePaths(databases=(),
files=(),owned_directories=(),owner_lock_directories=(),audit_files=())`, with
absolute paths. `live_control_plane_inventory(additional=trusted_callback)` returns
a fresh bounded snapshot. Root composition must supply the actual terminal output
store root and any additional policy/observation stores it really constructs;
this adapter does not invent a binding database. Current observation evidence is
inline in the existing fleet projection store. Artifact-private directories may
also be supplied from their actual typed-config resolver when composed.

`control_plane_scope(snapshot)` installs immutable supplemental paths for file
tools in that host execution context. Nested scopes accumulate protection and
restore it on exit, including exceptions. Default protection remains active.
There is no process-global mutable last-writer provider. This is a trusted internal
composition API and must never be exposed as model arguments. Context propagation
to worker threads remains the host's responsibility.

Inventory `protects(path)` handles exact files and owned directories; it does not
blanket-protect each database parent. `require_disjoint(model_roots)` is the stricter
host admission check: it rejects overlap in either direction with every private
file's anchoring directory and explicitly owned/lock directory. The snapshot is
bounded at 256 base entries and model roots, with bounded SQLite sidecar expansion.
Call the live provider again at admissions; a snapshot is not a grant or lease.
File tools still require their normal permission checks and path containment.
