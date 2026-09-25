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

### `--dist load` versus `--dist worksteal` (measured 2026-09-25)

CI keeps `--dist load`. Work-stealing was measured against it on a
2,347-test subset (`tests/production` plus `tests/test_r*`, `test_t*`,
`test_u*`) with `-n 4`, four runs each, interleaved back to back on a shared
4-CPU container:

| Mode | Runs (s) | Median |
|---|---|---:|
| `load` | 345, 297, 224, 306 | ~302 s |
| `worksteal` | 343, 342, 248, 324 | ~333 s |

Worksteal was slower in three of four adjacent pairs. The spread within one
mode (224-345 s) is wider than the gap, so read this as "not better here",
not as a precise penalty; re-measure before switching on other hardware.
`pytest-xdist>=3.2` is pinned because `scripts/test_fast.py` uses worksteal.

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

`scripts/test_fast.py` does the selection and the run in one step, with the
wrappers `scripts/test-fast.sh` and `scripts\test-fast.cmd` resolving the
interpreter the way `run-tests.cmd` does (`SONDER_PYTHON`, else the checkout's
`venv`):

```bash
scripts/test-fast.sh                  # change since merge-base(HEAD, origin/main)
scripts/test-fast.sh --since HEAD~3   # change since any ref
scripts/test-fast.sh --working-tree   # uncommitted edits only (vs HEAD)
scripts/test-fast.sh --all            # full suite, same flags
scripts/test-fast.sh -n 4 -- -x -k gate   # own options, then pytest's after --
scripts/test-fast.sh --dry-run        # print the pytest command only
```

It runs `pytest -n auto --dist worksteal --ff` (passthrough arguments come
after these, so they override them) and always prints the selector's
uncovered-identifier list: a green selected set says nothing about a changed
name no test mentions. A vacuous selection exits 2, as the selector does --
it is an infrastructure failure, never "nothing to run". When `origin/main`
is unavailable the selector's own default base is used.

## Slow tests that were waste (2026-09-25 capture)

A full `-n 3` capture ranked with `slow_tests.py` found two avoidable costs:

- `fanout_store`'s URI-credential redaction was quadratic on long letter
  runs; one 100k-character answer took ~90 s. `test_fanout_store.py` and
  `test_model_fanout.py` (~449 s of recorded time) now run in ~22 s wall.
- `test_app_recovery_http.py` polled with a fixed 10 s sleep; it now backs
  off from 0.5 s to the same cap (96 s -> 62 s back to back).

The rest of the top of the ranking is real work: nested processes, the
architecture checker on copied trees, and `app_control_http`'s private-scope
fingerprinting (`_private_scope_digest`, ~52k calls and ~76 s cumulative in
`test_managed_learning_principal_qualification.py` alone), which is a
product hot path, not test overhead.

## Where the fixed costs live

- **Collection (~19 s)** is dominated by importing 770+ test modules, most of
  which import `server` (a ~22k-line module). Since 2026-09-25 that no
  longer imports the `mcp` SDK: `server.mcp` is a
  `reloadable_mcp.LazyReloadableMCPServer` that records registrations and
  builds the real registry on first use (`tests/test_lazy_mcp_import.py`
  pins it). On the shared 4-CPU container, `import server` went from a
  2.66 s to a 1.02 s median (9 interleaved runs each; noisy host).
  `-k`/file selection does not avoid collection of the rest;
  pointing pytest at explicit files (as `select_regression_tests.py --format
  args` does) does.
- **Per-process interpreter setup** (`import server`, see above) is paid once
  per pytest process and once per xdist worker; it is why very small `-n`
  values amortize better than one worker per test file would.
