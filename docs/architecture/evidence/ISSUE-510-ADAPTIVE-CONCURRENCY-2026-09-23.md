# Issue #510 adaptive concurrency slice — 2026-09-23

Baseline: `origin/main` at `494f2397601b784abe82d5131693f16baad709d4`.
Audit and requirement mapping: `ISSUE-510-PARALLELISM-AUDIT-2026-09-23.md`.

This slice adds the missing adaptive half of issue #510 section 3, "bounded
productive parallelism", to the live delegated-fleet seam. It covers
ownership-aware admission and an automatic reduction of concurrency on retry
storms, churn, and resource pressure, with recovery growth.

## Policy (pure)

`sonder_runtime/domain/adaptive_concurrency.py` is domain-layer code. It uses
only the standard library, does no I/O, reads no clock, and has no threading
primitives.

- **Ownership.** `LaneClaim(lane_id, paths, access)` normalises paths: case is
  folded, separators are unified, and `..` and unanchored absolute paths are
  rejected. `anchor_claim_path(path, root)` expresses an absolute path
  relative to the project root, so `a.py` and `C:/repo/a.py` are one file; a
  path outside the root raises instead of becoming an independent lane. Two claims conflict
  when their paths overlap component-wise and at least one side writes.
  Readers never conflict with each other, so independent read fan-out keeps
  its full width. `admissible_lanes()` admits pending lanes in stable order up
  to the cap and skips any lane that conflicts with a running or admitted lane.
  There is no head-of-line blocking, and when nothing is running at least one
  lane is always admitted.
- **Adaptive cap.** `observe(state, outcome, policy, resources)` implements
  AIMD with hysteresis:
  - *Retry storm*: transient outcomes from 3 distinct lanes in an 8-outcome
    window. One flaky lane counts once, however often it retries.
  - *Churn*: 2 target-changed outcomes in the window.
  - *Resource pressure*: available memory below 10 %, or a `high`/`critical`
    band.
  - Any trigger halves the cap, down to the floor.
  - Consumed storm or churn evidence is cleared, so one burst shrinks once.
  - A persisting pressure breach shrinks again only after a 2-observation
    cooldown.
  - Between 10 % and 20 % (the hysteresis band) the cap holds: it neither
    shrinks nor grows.
  - Growth is +1, and only after 3 consecutive successes with pressure
    released. The cap never exceeds the ceiling.
  - An unknown resource reading never enters pressure. After 3 consecutive
    unknown readings, latched pressure is released, so a probe that goes dark
    cannot pin the cap. An unsampled observation (`None`) changes nothing.

## Live wiring

`master_orchestrator.run_delegated()` previously submitted every lane to a
fixed `ThreadPoolExecutor(max_workers=worker_slots)`. It now builds an
`AdaptiveLaneScheduler` from `delegated_lane_claims()` and dispatches through
`dispatch_lanes()`:

- The static `worker_slots` from `capacity()` is still the ceiling. Hardware,
  operator, and per-run caps are unchanged.
- The pool only receives admitted lanes, so a shrunk cap takes effect at the
  next admission.
- `_run_worker()` reports a content-free outcome through an optional
  `outcome_sink`:
  - `TRANSIENT_RETRY` for each transient retry or exhausted transient failure;
  - `CHURN` when provenance or target drift is detected before or after the
    model call;
  - `FAILED` for permanent failures and coverage misses;
  - `SUCCEEDED` otherwise.
- The memory signal is `concurrency_resources()`, measured from physical
  memory. On Linux/WSL it uses `MemAvailable` from `/proc/meminfo`, because
  `SC_AVPHYS_PAGES` is MemFree and excludes reclaimable page cache; the
  `sysconf` path remains the fallback. The fleet's own `fleet_pressure` band is deliberately excluded,
  because it measures the fleet's self-utilization.
- `dispatch_lanes()` logs and survives a raising `collect`/`on_error` (for
  example the fleet store failing while a failure is recorded), so later lanes
  are still admitted. If the loop itself aborts, every never-started lane is
  finished with `dispatch aborted before this lane started`, so no child row
  or reserved slot is stranded as queued.
- Lane claims are built inside the fleet-startup containment: an objective
  path that cannot be anchored fails the fleet with every queued child
  cancelled.
- Every non-hold decision emits a fleet event
  `adaptive concurrency shrink: cap 4 -> 2 (retry_storm)`. Every
  `run_delegated` return after dispatch (success, cancel, failure, drift, and
  audit paths) carries a `concurrency` summary with `ceiling`,
  `final_cap`, `peak_running`, `coupled_lanes`, `serialized_waits`, and
  `decisions`.
- Rollback: `SONDER_FLEET_ADAPTIVE_CONCURRENCY` accepts exactly
  `0`/`false`/`no`/`off` (pin the cap at `worker_slots`) or
  `1`/`true`/`yes`/`on`/unset. Any other value is logged as a warning and
  ignored.
- Delegated fleet lanes are read-only, so their claims are READ. Duplicate
  objectives on one file stay parallel. A future write-capable lane passes
  WRITE claims to the same scheduler.

No file in open PRs #519, #523, #525, #538, #541, or #542 is modified
except the shared append-only ledger and its generated projection.

## Verification

Focused tests (`tests/test_adaptive_concurrency.py`, 71 tests) cover:

- the pure ownership and cap policy;
- guard canaries that deliberately trip each shrink trigger:
  - *retry storm*, in the pure policy, the scheduler, and a live
    `run_delegated` run with 4 lanes timing out once each (cap 4 -> 2 and the
    fleet event);
  - *churn*, in the pure policy and the scheduler;
  - *resource pressure*, in the pure policy (with cooldown and hysteresis),
    the scheduler (sampling on a silent/cancelled completion), and a live
    `run_delegated` run with 3 % free memory;
  - *ownership serialization*, through `dispatch_lanes` with real threads:
    overlapping writers never overlap, while independent lanes run 3-wide
    behind a barrier;
- the rollback switch;
- a healthy fan-out that keeps full width (3 lanes behind a barrier, no
  decisions);
- review fixes for PR #552, each with a test that failed before its fix (17
  failed before, all pass after): dark-probe pressure release, a raising
  error handler, never-started lanes on abort, `run_delegated` with the fleet
  store failing, Linux `MemAvailable` from a fake meminfo, path anchoring and
  rejection, one-retry-per-lane, the `concurrency` key on the cancel and
  all-failed paths, and strict kill-switch parsing.

Mutation proof: each deliberate break was applied and the file rerun, and each
break made the suite fail.

| Mutation | Result |
|---|---|
| Shrink disabled | 13 failed |
| Conflicts disabled | 6 failed |
| Worker retry reporting removed | 1 failed (the live retry-storm canary) |
| Live wiring forced non-adaptive | 2 failed |
| Memory probe forced unknown | 2 failed |

Local runs (Windows 11, Python 3.12.10, pinned `requirements-runtime.txt` and
`requirements-dev.txt`):

```text
python -m pytest -q tests/test_adaptive_concurrency.py
71 passed
python -m pytest -q <orchestrator, provenance, durability, readiness, fleet-pattern and adaptive modules>
214 passed
python -m pytest -q -n auto <every module referencing master_orchestrator, fleet_pressure or adaptive_concurrency, except test_serve_auth.py>
890 passed (three consecutive runs)
python scripts/check_architecture.py            (exit 0)
```

## Limitations

- The cap is per run, not process-wide, so a storm in one fleet does not
  narrow the next fleet.
- Outcomes fold at the next lane completion. In-flight lanes are never
  pre-empted.
- Only the delegated-fleet dispatcher is adaptive. Subagent budgets, durable
  fanout, and compute-fabric reservations remain static.
- No production write-capable lane exists yet, so write serialization is
  proven only through `dispatch_lanes` tests.
- The policy thresholds are defaults and have not been tuned against a
  measured workload.
- Status is `implemented_unverified`. There is no CI run on a merged SHA.
