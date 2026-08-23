# Test-Suite Performance

How to run the suite fast, find what makes it slow, and see a hung test
instead of a stuck terminal. Companion pages: [Benchmarking](17-benchmarking.md)
measures the *runtime's* value; this page measures the *suite* itself.

## Baseline (measured 2026-08-22, Ryzen 9 9900X3D, Python 3.12, pytest 9.1)

| What | Cost |
|---|---|
| Collection alone (`--collect-only`, 10,118 tests) | ~19 s |
| `import server` (cold) | ~1.06 s, of which the `mcp` package is ~0.43 s |
| Sum of per-test time, full suite (10,086 tests run) | ~1,012 s (≈17 min) — the serial floor |
| Full suite as ten file-chunks under `-n 4` | ~410 s wall total (≈7 min) |
| `scripts/check_architecture.py` | ~3.5 s (was ~18 s before the parse-once index) |
| Retired-root ratchet test | ~24 s (was ~18 *minutes* as a 45-way parametrize) |
| Pool scheduler overhead (`scripts/benchmark_worker_pool.py`) | ~1.4 µs/request |

Numbers were taken with other workloads on the machine; treat them as
indicative, not laboratory-grade. Re-measure on your own hardware with the
commands below before drawing conclusions from a delta.

## Finding slow tests

Capture per-test timings as data instead of scrollback:

```powershell
$env:SONDER_TEST_TIMINGS = "timings.jsonl"
scripts\run-tests.cmd -q
python scripts\slow_tests.py timings.jsonl
```

`slow_tests.py` ranks the slowest tests (and the costliest test *files*, the
unit xdist actually schedules), and `--compare old.jsonl` reports per-test
regressions between two captured runs. The capture costs nothing when the
variable is unset, and under xdist each worker writes `timings.jsonl.gwN`,
which the reader merges automatically.

An empty capture makes `slow_tests.py` exit 2 with a loud message. That is
deliberate: a timing file that was never written looks exactly like a fast
suite, and must not be read as one.

## Hung-test visibility

`pytest.ini` sets `faulthandler_timeout = 300`. A test that exceeds five
minutes gets every thread's traceback dumped to stderr while the run
continues -- so a wedged test identifies itself instead of being discovered by
killing the run and losing the evidence. If a legitimately slow test ever
approaches the limit, raise the limit in `pytest.ini` alongside a timing
capture proving the test's cost, and update this page.

## Bounded parallel execution

`pytest-xdist` is already a dev dependency, and the suite is xdist-clean:
the whole suite was validated green under `-n 4 --dist load` on 2026-08-22
(both hermetic-state conftests allocate per-process roots, and the HTTP tests
bind ephemeral ports). Running it that way roughly halved-to-thirded the
wall clock on a loaded 12-core machine:

```powershell
scripts\run-tests.cmd -q -n 4
```

Keep parallelism bounded (`-n 4` rather than `-n auto`) when other builds or
agent fleets are running; the suite spawns real subprocesses in places, so
worker count understates process count.

Parallel runs are also a flakiness detector: each worker starts with cold
process state, so a test that only passes because an earlier test warmed a
cache fails immediately under xdist. That is how the `/api/show`
metadata-probe order-dependence in the extraction and timeout tests was
found (fixed 2026-08-23) — treat a test that fails under `-n 4` but passes
serially as a real bug in the test, not as a reason to avoid parallelism.

## Running less: selecting tests from a change

`scripts/select_regression_tests.py` derives a regression set from the
identifiers your diff actually touched, and reports which changed identifiers
no test covers at all. Use it for iteration; run the full suite before
merging.

```powershell
python scripts\select_regression_tests.py --format args | % { scripts\run-tests.cmd -q $_.Split(" ") }
```

## Where the fixed costs live

- **Collection (~19 s)** is dominated by importing 770+ test modules, most of
  which import `server` (a ~22k-line module) and, transitively, the `mcp`
  package. `-k`/file selection does not avoid collection of the rest;
  pointing pytest at explicit files (as `select_regression_tests.py --format
  args` does) does.
- **Per-process interpreter setup** (~1 s for `import server`) is paid once
  per pytest process and once per xdist worker; it is why very small `-n`
  values amortize better than one worker per test file would.
