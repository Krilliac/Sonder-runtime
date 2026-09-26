# Recorded cancellation and process reaping

`SubprocessJobProvider.wait()` previously classified an exited process as
successful or failed when native containment was absent, even if the durable
job already recorded CANCELLATION_REQUESTED. It could also release the local
process and capacity while cancellation cleanup remained unproven.

The provider now routes that recorded cancellation through its existing
cleanup contract. Incomplete cleanup retains the cancellation state, owned
process, capacity and bounded retry timer. Proven cleanup yields CANCELLED.
Natural exits without a recorded cancellation retain their normal exit mapping.

After the provider reaps a POSIX root, the supervisor can confirm that its
recorded root-owned process group is absent. It requires a recorded process
identity, a dead-root observation, and a group ID equal to that root PID. The
only OS operation in this path is `killpg(group, 0)`: ProcessLookupError proves
absence. A live group, denied/failed probe, unknown root or mismatched identity
does not authorize termination or claim cleanup.

The liveness API can report DEAD for the original owner while returning the
identity of a replacement at that PID. The absence check explicitly refuses
that replacement identity before querying the group.

Local qualification:

- Native Windows provider, capacity and restart cohort: 83 passed, three
  explicit platform skips. Separate supervisor/adapter cohort: 19 passed.
- Final Linux combined cohort: 105 passed, one Windows Job Object skip. This includes
  real single-child Popen execution, recorded process identity, SQLite reopen,
  and cancellation preserved for zero and nonzero process exits.
- Deterministic tests retain capacity during incomplete cleanup, retry to
  completion, preserve ordinary exits, and refuse live/unknown/reused groups.
  The initial cancellation-priority regressions failed before the correction.
- Workflow pinning/retirement checks: 15 passed. New tests are Ruff-clean;
  adapter findings remain at 18 baseline/current, with no added diagnostic.
- Final full Linux suite: 17,523 passed, 175 skipped, three warnings and four
  passing subtests in 438.73 seconds. The snapshot used canonical Git content
  at 6b31a39c plus the candidate patch; all 3,544 source files matched before
  qualification and no cross-platform bytecode was copied.

This is a focused OPS-005 correction. It does not qualify completion of a live
group kill, escaping descendants, Windows tree absence after root exit, or the
larger held startup/identity-propagation candidate. Those broader gates remain
open and OPS-005 remains implemented_unverified. The existing finite graceful
drain deadline validation remains part of the requirement's implementation.
