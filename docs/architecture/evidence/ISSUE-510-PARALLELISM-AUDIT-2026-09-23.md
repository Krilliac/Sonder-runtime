# Issue #510 section 3 parallelism audit — 2026-09-23

Audited against `origin/main` at `494f2397601b784abe82d5131693f16baad709d4`.

Issue #510 section 3, "Bounded productive parallelism", asks for four things:

1. concurrency caps and ownership-aware scheduling;
2. aggressive delegation where work is independent;
3. an automatic reduction of concurrency during churn, retry storms, resource
   pressure, or tight coupling;
4. an optimisation target of useful independent lanes, not maximum live-agent
   count.

## Requirement mapping

| Master-spec requirement | Relation to section 3 | Claimed by this lane |
|---|---|---|
| `AGENT-007` Budgets (depth, child count, **concurrency**, tokens, time, model, execution resources) | Direct: concurrency caps and resource budgets | Yes: revision 3, `implemented_unverified` |
| `AGENT-008` Isolated workspaces; reconcile concurrent Git changes without force-overwriting | Ownership-aware scheduling is the admission half: overlapping writers must not run together | No. The live fleet seam is read-only (see below), so no write lane exercises it yet |
| `AGENT-002` Migrate existing modes to the shared contract | The fleet dispatcher is still a legacy root-module loop | No |
| `JOB-001` / `JOB-002` Generic job registry and control | Section 3 caps should eventually apply to every job kind, not only fleet lanes | No |
| `OPS-003` Health model (degraded capability) | A shrunk cap is a degraded-capability signal worth projecting | No |
| `OPS-005` Graceful drain | Drain stops admission entirely; section 3 only narrows it | No (existing) |

## What already exists

| Control | Location | What it does | Gap against section 3 |
|---|---|---|---|
| Static fleet worker slots | `master_orchestrator.capacity()` | `min` of requested agents, CPU (`logical // 2`, max 8), free RAM (`(available - 1.5 GiB) / 0.25 GiB`), GPU KV headroom, `OLLAMA_NUM_PARALLEL`, reserved slots; operator ceiling `SONDER_MAX_WORKER_CAP`, per-run `worker_cap`, legacy `SONDER_PARALLEL_WORKERS`; hard ceiling 64 | Computed **once** at run start. Nothing narrowed it mid-run on retries, churn, or memory falling |
| Fleet pool | `master_orchestrator.run_delegated()` | `ThreadPoolExecutor(max_workers=worker_slots)`; every lane was submitted up front | No ownership awareness; no feedback from worker outcomes |
| Bounded transient retry | `master_orchestrator._run_worker()`, `classify_worker_error()`, `worker_transient_retries()` | One extra attempt (0..3 via `SONDER_FLEET_TRANSIENT_RETRIES`) for `timeout`/`unavailable`/`throttled`/`transport` | Each lane retried independently; a fleet-wide retry storm kept full width |
| Provenance drift checks | `fleet_provenance.validate_delegation()` before and after the model call | Detects that an objective's target file changed during the call (`task_drift`) | Churn was recorded per lane but never fed back into scheduling |
| EWMA pressure tracker | `sonder_runtime/domain/fleet_pressure.py`, updated in `master_orchestrator._notify_snapshot_subscribers()` | Hysteresis-banded utilization of the fleet's own slots | **No production reader.** `fleet_pressure_band()`/`fleet_pressure_sample()` are only referenced by tests. It also measures self-utilization, so a healthy full-width run reads as `critical`; it is unsuitable as a shrink signal |
| Backpressure chain | `sonder_runtime/domain/backpressure.py` | Hash-based probabilistic admission from a pressure band | **No production consumer** (tests only) |
| Subagent concurrency budget | `SubagentBudget.max_concurrency` (`application/ports/subagents.py`), enforced in `application/subagents/durable_continuation.py` | Rejects a new child when the root's active children reach the budget | Static cap; rejection rather than queueing; no ownership or feedback |
| Worker capacity reservations | `application/compute_fabric/capacity.py`, `adapters/persistence/sqlite/worker_capacity.py` | Durable per-host memory/job-count reservations with leases | Compute-fabric jobs only; static budget |
| Durable fanout | `server.py` (`_fanout_start`, fanout run loop), `domain/fanout_admission.py` | Local models run serially for VRAM safety; cloud rows use at most 2 workers | Static; correct for its purpose |
| Modality scheduler | `domain/multimodal_scheduler.py` | Pure local-first VRAM/RAM packing for modality jobs | Only used by `scripts/package_local_system.py` |
| Runtime admission gate | `application/operations/admission_gate.py`, `adapters/web/lifecycle.py` | Binary accept/stop for graceful drain | Binary; out of scope for narrowing |

## Findings

- **Caps existed; adaptation did not.** Every cap was fixed at run start, so a
  retry storm, a churning target, or falling host memory never narrowed a
  running fleet.
- **No ownership model in the scheduler.** Objectives carry a `file:` path,
  and `_objective_assignments()` deliberately duplicates objectives when there
  are more agents than objectives. That is safe today only because delegated
  fleet workers get guarded **read-only** tools (see `_subtask_prompts`). No
  write-capable parallel lane goes through this dispatcher yet.
- **A dead pressure signal.** `fleet_pressure` and `backpressure` look like
  adaptive admission, but nothing in production reads them. That is a guard
  that silently does nothing. They also measure the wrong thing for this
  purpose.

## Slice implemented by this lane

See `ISSUE-510-ADAPTIVE-CONCURRENCY-2026-09-23.md`. Summary:

- `sonder_runtime/domain/adaptive_concurrency.py`: a pure policy covering
  ownership conflicts (read/write claims, component-wise path overlap) and an
  AIMD cap. A retry storm, churn, or memory pressure halves the cap. Growth is
  one step at a time, only after a healthy streak with pressure released. The
  memory signal has enter and exit thresholds (hysteresis) plus a cooldown.
- `master_orchestrator.AdaptiveLaneScheduler` and `dispatch_lanes()`: the live
  `run_delegated` seam now admits only lanes that the policy allows. It is still
  bounded by the unchanged static `worker_slots` ceiling.

## Remaining gaps (not in this slice)

- The cap is per run. No cross-run or process-wide concurrency memory exists,
  so a storm in one fleet does not narrow the next fleet.
- Retry events are folded at the next lane completion, not the moment they
  happen. That is sufficient for admission, since only completions free slots,
  but in-flight lanes are never pre-empted.
- The durable-continuation subagent path (`SubagentBudget.max_concurrency`),
  the fanout loop, and compute-fabric reservations are not yet wired to the
  adaptive policy.
- The shrunk cap and its reasons appear only in fleet events and the
  `run_delegated` result. They are not yet projected into `OPS-003` health.
- `fleet_pressure` / `backpressure` are left as-is, still without a production
  consumer. Retiring or rewiring them needs its own decision.
- Write-capable parallel lanes (the `AGENT-008` reconcile path) do not exist in
  this dispatcher. The ownership serializer is proven with write claims through
  `dispatch_lanes` but has no production write lane yet.
