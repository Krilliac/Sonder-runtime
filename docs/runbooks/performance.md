# Performance measurement and safe acceleration

Measure a repeatable boundary before changing it. Keep heavyweight runs
serialized on memory-constrained workstations, and do not remove assertions,
timeouts, privacy checks, or permission gates to improve a timing number.

## Test profiling

The profiling harness runs pytest and atomically writes a bounded JSON report:

```bash
python scripts/profile_tests.py tests/test_admission_fairness.py
python scripts/profile_tests.py --since main
python scripts/profile_tests.py --since main --workers 2
```

Serial is the default. Parallel mode is explicit, capped at four xdist workers,
and uses `--dist loadfile` so tests from one file stay together. The report at
`.pytest_cache/sonder-test-profile.json` contains only node IDs, outcomes,
durations, counts, worker limits, and the slowest 25 tests by default. It never
captures stdout, exception text, environment variables, source, request data,
or model prompts. Use `--top 1..200` to change the bounded leaderboard and
`--output PATH` to choose another artifact.

`--since REV` invokes `select_regression_tests.py`. The selector derives terms
from changed module-level identifiers, always includes directly changed test
files (including untracked Python files up to 4 MiB), and fails closed when a
diff produces no runnable coverage or an untracked source exceeds the bound. Its JSON
diagnostic is available independently:

```bash
python scripts/select_regression_tests.py --since main --format json
```

The selected set is for fast iteration. Run the complete suite before merge.

## Architecture gate profiling

The architecture checker can report its content-free inventory and timing:

```bash
python scripts/check_architecture.py --stats
```

The JSON diagnostic includes tracked files, package files, parsed files,
compatibility-rule count, violations, and elapsed seconds. ASTs and source text
are cached only in memory for one invocation. A persistent cache is deliberately
not used because stale results could hide a newly introduced forbidden import.

Reference measurement on Windows/Python 3.12 (2026-08-22): the compatibility
rules previously reparsed 844 files for each of ten rules, producing 9,080 AST
parses and 10.9 million AST walks. `cProfile` measured 58.111 seconds. A shared
per-invocation import index reduced the same profiled check to 851 parses, 1.04
million AST walks, and 9.664 seconds (6.0x faster); the unprofiled check took
3.061 seconds. Re-measure on the target machine; these numbers are a regression
reference, not a universal SLA.

The 71-test architecture module previously remained incomplete after 270
seconds because every retired-path parameter copied and staged the full package.
The ratchet is now a pure function exercised for every one of the same 45 paths,
while separate copied-repository tests retain end-to-end failure coverage. The
complete serial module now takes 41.073 seconds on the reference machine.

## Runtime admission telemetry

`RuntimeLifecycle.admission_telemetry_snapshot()` exposes content-free resource
state: configured active/queue limits, active and queued request counts,
available slots, queue high-water mark, aggregate wait samples/times, and fixed
rejection-code counts. The same snapshot appears under
`/health -> admission -> resources`.

Prometheus adds these fixed-cardinality series when metrics are enabled:

- `sonder_admission_capacity{kind="active|queue"}`
- `sonder_admission_queue_depth`
- `sonder_admission_queue_high_watermark`
- `sonder_admission_queue_wait_seconds`
- `sonder_admission_rejections_total{reason="..."}`

Owner keys, correlation IDs, request content, and free-text errors are never
labels or snapshot fields. Metrics remain optional and become no-ops when the
Prometheus client is absent or disabled.
