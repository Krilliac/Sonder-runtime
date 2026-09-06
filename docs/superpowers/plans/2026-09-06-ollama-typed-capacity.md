# Typed Ollama Capacity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make validated `[ollama]` capacity settings authoritative for typed runtime composition, even when stale compatibility environment variables are present.

**Architecture:** The typed worker registry in `ollama_pool` already overrides worker origins, remote consent, trusted origins, timeouts, and circuit settings. Extend that registry with the two missing admission bounds and select them before reading the environment. Every typed composition root passes the two parsed `OllamaConfig` values. Legacy callers with no typed registry continue to obtain both settings from their injected environment.

**Tech Stack:** Python 3.12, pytest, typed runtime configuration, stdlib-only Ollama worker pool.

**Spec:** `docs/runbooks/multi-node-ollama.md`, `docs/wiki/03-configuration.md`, and `sonder_runtime/platform/config.py` `OllamaConfig` validation.

## Global Constraints

- Preserve the bounded request-level pool: at most 16 total workers, `worker_max_inflight` 1..64, and one shared `worker_queue_depth` 1..4096 from typed configuration.
- Preserve legacy environment behavior only when no typed worker configuration is active or an explicit environment mapping is passed to `from_environment`.
- Do not add model sharding, cross-node job replay, automatic takeover, or unbounded scaling claims.
- Do not change runtime installation state or introduce network/model-dependent tests.

---

### Task 1: Make typed capacity authoritative in the pool factory

**Files:**
- Modify: `sonder_runtime/adapters/inference/ollama_pool.py:225-287,1234-1323`
- Modify: `tests/test_ollama_pool.py:202-216`

**Interfaces:**
- Consumes: `configure_typed_workers(worker_origins, *, allow_remote, ..., max_inflight_per_worker: int | None, queue_depth: int | None)`.
- Produces: `from_environment(primary_origin)` builds `OllamaWorkerPool` with the configured typed maximum inflight count and shared queue depth when typed configuration is active.

- [x] **Step 1: Write the failing stale-environment test**

```python
def test_typed_capacity_is_authoritative_without_environment_round_trip(monkeypatch):
    try:
        configure_typed_workers(
            ("https://worker.example:443",),
            allow_remote=True,
            max_inflight_per_worker=7,
            queue_depth=90,
        )
        monkeypatch.setenv("SONDER_OLLAMA_WORKER_MAX_INFLIGHT", "not-an-int")
        monkeypatch.setenv("SONDER_OLLAMA_WORKER_QUEUE_DEPTH", "still-not-an-int")
        pool = from_environment("http://127.0.0.1:11434")
        assert {row["capacity"] for row in pool.status()["workers"]} == {7}
        assert pool.status()["queue"]["limit"] == 90
    finally:
        reset_typed_workers()
```

- [x] **Step 2: Run the focused test to verify the existing bug**

Run: `python -m pytest -q tests/test_ollama_pool.py::test_typed_capacity_is_authoritative_without_environment_round_trip`

Expected: FAIL because `from_environment()` still consults invalid stale environment values instead of applying typed limits `7` and `90`.

- [x] **Step 3: Store and select the two typed values**

Add `_configured_max_inflight` and `_configured_queue_depth`, reset both in `reset_typed_workers()`, accept both keyword-only parameters in `configure_typed_workers()`, and snapshot both under `_configuration_lock`. In `from_environment()`, use each typed value when `use_typed` is true and the value is not `None`; otherwise retain the existing `_positive_int()` environment fallback. Pass the selected values to `OllamaWorkerPool` unchanged.

- [x] **Step 4: Run the focused pool test to verify the fix**

Run: `python -m pytest -q tests/test_ollama_pool.py::test_typed_capacity_is_authoritative_without_environment_round_trip tests/test_ollama_pool.py::test_explicit_environment_remains_an_injectable_compatibility_boundary`

Expected: PASS. The typed path ignores invalid stale process environment values; an explicit environment-map compatibility path, even after typed values have been configured, remains environment-driven and selects its own distinct capacity values.

- [x] **Step 5: Commit the pool behavior and regression test**

```powershell
git add -- sonder_runtime/adapters/inference/ollama_pool.py tests/test_ollama_pool.py
git commit -s -m "fix(inference): honor typed Ollama capacity bounds"
```

### Task 2: Propagate typed bounds through every canonical composition root

**Files:**
- Modify: `sonder_runtime/__main__.py:652-663,826-837`
- Modify: `sonder_runtime/bootstrap/app.py:378-389`
- Modify: `tests/test_typed_runtime_export.py`

**Interfaces:**
- Consumes: `OllamaConfig.worker_max_inflight` and `OllamaConfig.worker_queue_depth`, both already validated by `load_config()`.
- Produces: Each typed serve, legacy-MCP, and application-bootstrap call to `configure_typed_workers()` includes `max_inflight_per_worker=config.ollama.worker_max_inflight` and `queue_depth=config.ollama.worker_queue_depth`.

- [x] **Step 1: Write composition capture tests**

Use the existing monkeypatch seams to replace `ollama_pool.configure_typed_workers` with a capture function, build a config whose values are `7` and `90`, invoke each root, and assert the capture includes:

```python
{
    "max_inflight_per_worker": 7,
    "queue_depth": 90,
}
```

The test must cover `cmd_serve`, the legacy MCP setup callback, and `build_application(config=...)`; no real endpoint or worker probe may run.

- [x] **Step 2: Run the new composition tests to verify they fail**

Run: `python -m pytest -q tests/test_typed_runtime_export.py`

Expected: FAIL because the captured calls omit both keyword arguments.

- [x] **Step 3: Forward the two values at all three roots**

Add exactly the two keyword arguments to the calls at the listed `__main__.py` and bootstrap locations. Do not rely on `_export_runtime_environment()` to make canonical typed composition correct.

- [x] **Step 4: Run the typed composition regressions**

Run: `python -m pytest -q tests/test_typed_runtime_export.py tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py tests/test_operational_capabilities.py`

Expected: PASS with no transport calls to a live Ollama worker.

- [x] **Step 5: Commit the composition wiring**

```powershell
git add -- sonder_runtime/__main__.py sonder_runtime/bootstrap/app.py tests/test_typed_runtime_export.py
git commit -s -m "fix(runtime): pass typed Ollama capacity to worker pool"
```

### Task 3: Correct shared-queue operator wording and validate generated documentation

**Files:**
- Modify: `docs/runbooks/multi-node-ollama.md:180-186`
- Check: `docs/wiki/03-configuration.md:62-67`
- Check: `docs/architecture/generated/runtime-reference.{md,json}`

**Interfaces:**
- Consumes: `OllamaWorkerPool._waiters` and `_queue_depth`, which are one pool-wide counter and limit.
- Produces: Operator documentation calls `worker_queue_depth` a bounded waiter limit across the pool and leaves `worker_max_inflight` described as a per-worker limit.

- [x] **Step 1: Write the documentation expectation in the runbook**

Replace the table entry `Backpressure queue per worker.` with `Bounded backpressure waiters across the pool.` This aligns the runbook with the existing configuration wiki and implementation.

- [x] **Step 2: Run the documentation and architecture checks**

Run: `python scripts/generate_documentation_catalogs.py --check; python scripts/check_doc_links.py; python scripts/check_architecture.py`

Expected: all commands exit zero; no generated catalog change is required unless the source schema changed.

- [x] **Step 3: Run the full focused verification set**

Run: `python -m pytest -q -n auto --dist load tests/test_ollama_pool.py tests/test_ollama_pool_distributed.py tests/test_typed_runtime_export.py tests/test_operational_capabilities.py tests/test_compute_placement_policy.py`

Expected: PASS. This proves configuration, bounded admission, capability routing, and placement policy behavior; it does not prove a live multi-node deployment, model sharding, takeover, or indefinite scale.

- [x] **Step 4: Commit documentation and validate the final diff**

```powershell
git add -- docs/runbooks/multi-node-ollama.md
git diff --check
git commit -s -m "docs(inference): clarify shared pool queue bound"
```

## Self-Review

- Typed values are chosen before environment fallback only for canonical typed composition; explicit injected environments remain testable compatibility inputs.
- All three canonical configuration roots carry both bounds.
- The plan preserves request-level pooling, current upper bounds, and the existing safe retry policy.
- The documentation wording matches the single `_waiters` / `_queue_depth` implementation.
