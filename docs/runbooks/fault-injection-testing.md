# Deterministic fault-injection testing

Sonder reliability tests inject failures at dependency seams; they do not
depend on wall-clock sleeps, live models, real network outages, filesystem
quotas, or killing operator processes. The shared fixtures live in
`tests/fixtures/fault_injection.py`, and the cross-boundary scenarios live in
`tests/test_failure_injection.py`.

## Covered contracts

| Failure | Injection seam | Required assertion |
| --- | --- | --- |
| Network/model failure | finite scripted transport/provider call | stable domain error, bounded call count, no response publication |
| Timeout/cancellation race | cancellation token flipped inside the dependency call | late result is discarded after exactly one call |
| Worker crash | Popen-shaped scripted process | non-zero/crashed work cannot become successful |
| Output-store failure | durable registry `append_output` seam | process success is downgraded to a safe failed record without leaking the storage message |
| SQLite lock | matching SQLite statement fault | `CONCURRENCY_CONFLICT`; a later operation can retry |
| Disk full | matching SQLite write fault | `CAPACITY_EXCEEDED`; the transaction leaves no partial job |
| Malformed cleanup response | process supervisor receipt | wrong identity or overstated completion is rejected before state mutation |
| Restart recovery | reopened SQLite registry plus cleanup supervisor | only a complete, identity-matched receipt marks orphaned work interrupted |

The SQLite connector is constructor-injected only for tests. Production still
uses `sqlite3.connect`, a five-second busy timeout, foreign keys, and the same
database path. Errors are classified without returning SQLite messages or
paths to callers:

- busy/locked: `CONCURRENCY_CONFLICT` (retryable);
- full/no space: `CAPACITY_EXCEEDED` (retryable after capacity is restored);
- corrupt/not-a-database: `INTEGRITY_FAILURE` (do not retry in place);
- other SQLite or OS failures: `DEPENDENCY_UNAVAILABLE`.

## Adding a scenario

1. Inject at an existing constructor or port seam. Do not add a production
   environment flag that can enable faults in a live runtime.
2. Use `ScriptedCall` for an exact sequence of returns, callbacks, and raises.
   Assert both `calls` and `remaining` so accidental retries are visible.
3. Use `MutableCancellationToken` to flip cancellation from inside the mocked
   dependency. This deterministically covers the return/cancel race.
4. Use `SQLiteFaultConnector.fail_next` with `statement_contains` for one exact
   database operation. Do not fill a real disk or lock an operator database.
5. For process behavior, use `ScriptedProcess`; never signal an unrelated host
   process from a unit test.
6. Assert durable state after the failure and after one safe retry/reopen.
   Error type alone is insufficient evidence of rollback or recovery.

## Focused verification

Run tests serially on constrained hosts:

```text
python -m pytest -q tests/test_failure_injection.py
python -m pytest -q tests/test_model_gateway_conformance.py tests/test_openai_compat_gateway.py
python -m pytest -q tests/test_sqlite_job_registry_port.py tests/test_api003_restart_recovery.py tests/test_job004_process_provider.py
```

Live model and network tests remain separately marked. Fault-injection tests
must stay under the `unit` marker and must pass while offline.
