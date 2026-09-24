# Issue #510 worker execution contract evidence

Status: `implemented_unverified`

This slice adds a typed `WorkerExecutionContract` to delegated worker requests.
It records bounded success criteria and deterministic argv command declarations,
persists them in the existing durable child-session request, restores them after
restart, and makes `DelegationService.integrate` reject successful results when
criteria or declared command identity do not match the persisted contract.
Terminal verification retains the criteria and declarations alongside the
result record.

## Context policy and ownership (second slice)

The contract now also carries:

- `context_policy` (`WorkerContextPolicy`): `inherit` requires one lowercase
  SHA-256 `inherited_context_sha256`; `scoped` requires a non-empty list of
  explicit `WorkerContextInput(reference, sha256)` values; `clean` forbids both;
  `unspecified` (the legacy default) forbids both. A reference pinned to two
  different digests is rejected.
- `owned_files`: paths the worker exclusively mutates. The contract itself
  rejects relative paths and `..` segments, resolves each path
  (`Path.resolve`, which follows symlinks and expands Windows short names for
  existing components), applies `os.path.normcase`, and stores `/`
  separators, so a launch admitted directly through the registry cannot bypass
  canonicalization. `DelegationService.dispatch` additionally requires each
  owned file to be permitted by the child's write assignment before registry
  admission or provider spawn.
- `task_scope`: the logical task the worker owns, distinct from owned files and
  from `WorkerLaunch.scope` (the readable workspace roots).
- `speculative_lane`: an explicit opt-in that lets two active workers share a
  `task_scope`; speculative lanes may not own files.

`ContinuationWorkerRegistry.admit` rejects a new reservation with
`DuplicateWorkerError` when an active child session already owns an
overlapping owned path (equal or ancestor, compared exactly on the canonical
strings; case folding happens only through `os.path.normcase`, so case variants
conflict on Windows while case-distinct POSIX files do not) or the
same non-speculative `task_scope`. Terminal children release ownership. All
contract fields persist as canonical `execution_*` metadata in the single
`durable_child_session` row; rows written by the first slice (criteria and
commands only) restore with `unspecified` policy, and any malformed field fails
closed with `WorkerRegistryError`. Terminal verification records the policy,
input digests, inherited digest, owned files, and task scope. Integration always
rejects a request whose contract (including the inherited-context digest)
differs from the persisted one; the success-criteria and command-match gate
applies only to a succeeded result, so a failed or interrupted worker with a
contract still records its failure status and output digest.

Evidence:

- `sonder_runtime/application/ports/worker_registry.py`
- `sonder_runtime/application/agents/lineage_delegation.py`
- `sonder_runtime/application/agents/delegation_service.py`
- `sonder_runtime/application/worker_registry/continuation.py`
- `tests/test_continuation_worker_registry.py` (48 tests, including 11
  validation cases, restart round-trip, inherited-digest drift, owned-file
  write-assignment gate, owned-file overlap, task-scope/speculative lanes,
  legacy rows, 7 malformed-metadata cases, failed-worker-with-contract,
  relative-path registry bypass, symlinked spellings, exact canonical overlap,
  and platform-specific case tests: the POSIX case-distinct test skips on
  Windows and the Windows case-variant test skips on POSIX)
- `python -m pytest -p no:cacheprovider -q tests/test_worker_registry.py tests/test_remaining_agent_004_008_009.py tests/test_continuation_worker_registry.py tests/test_delegated_verification.py tests/test_remaining_agent_010.py tests/test_workflows.py`
  (Windows: `103 passed, 1 skipped`; the skip is the POSIX-only case test)
- Load-bearing check: disabling the active-session scan in
  `_reject_ownership_conflict` fails the overlap and task-scope tests; removing
  the dispatch write-assignment check fails the write-assignment cases.
- Review regressions (fail before the fix at 15cf5e2e): a FAILED result with
  `success_criteria` raised `worker execution criteria were not verified`
  instead of recording failure; `WorkerExecutionContract(owned_files=("src/a.py",))`
  was accepted; a symlinked spelling of an owned file did not compare equal to
  its target.

## Atomic launch/start/checkpoint/finish

Current single-store facts (verified by reading the code, not by a new test):
reservation (`admit` -> `create`), start, checkpoint (`save_checkpoint` with
`expected_sequence`), terminal result, and `record_verification` are all
mutations of one `durable_child_session` row in `child-sessions.db`, each a
single `BEGIN IMMEDIATE` prepared mutation with revision/sequence
compare-and-set and `reconcile` for commit-ambiguous writes. The worker
lifecycle therefore needs no cross-database transaction.

The remaining cross-store gap is the subagent effect journal
(`worker-effects.db`, run id `subagent:<child_id>`), which ADR-003 keeps in a
separate file. A crash between a journaled effect outcome and the next child
checkpoint leaves the journal ahead of the checkpoint. Designed saga (not
implemented here because it touches `effect_journal.py` and
`worker_bindings.py`, owned by the PR #523 lane):

1. Before each checkpoint CAS, the runner reads the journal's settled
   high-water sequence for its run and stores it as `effect_high_water` in the
   checkpoint state (same CAS as the checkpoint; no second write).
2. On resume or `recover_after_restart`, journal records above the persisted
   high-water are classified: settled records with receipts are replayed by
   idempotency key (returning the stored receipt, never re-invoking); intents
   without an outcome or marked uncertain set `recovery_required` and refuse
   restart, which `AuthenticatedWorkerBinding.recover_before_restart` already
   enforces for uncertain effects.
3. A terminal success is written only when the journal holds no unresolved
   intents for the run; otherwise the child is finished with
   `recovery_required=True` and needs the explicit resume path.

Required dependency: a read-only journal API returning the settled high-water
sequence for one run id. Until it exists, checkpoints do not bind the journal
position.

Limitations:

- The contract validates and records exact command argv declarations; this slice
  does not execute commands, inspect exit status, or bind a host-owned verifier
  receipt. Supplied tuples therefore do not prove that a command ran or passed.
  A later verifier integration must provide that authority in the appropriate
  workspace.
- Context inputs and the inherited-context digest are declared and durably
  bound, but this slice does not compute or re-hash the parent context or input
  contents; the caller supplies the digests.
- Owned-path canonicalization touches the filesystem at contract construction
  (including every restore from persisted metadata): `Path.resolve` queries
  existing path components, so an owned path on an unreachable UNC share or
  disconnected mapped drive can stall construction for the OS network timeout.
  Owned files should live on local workspace volumes.
- Owned-path canonicalization reflects the filesystem when the contract is
  built or restored; a symlink created or retargeted later can change the
  canonical form, and a restored contract would then fail the equality gate
  closed rather than silently match.
- Owned-file and task-scope exclusivity is serialized by a process-wide lock
  around the active-session scan and create. Stable-key uniqueness remains an
  atomic SQLite check, but ownership exclusivity is not a database constraint,
  so two processes sharing one `child-sessions.db` could still race.
- Hosted CI, external provider qualification, and post-merge evidence remain
  unverified.
