# Structured test runs (`test_run`)

`test_run` runs a project's test suite and returns a typed report instead of
raw output. The report holds totals, failures with `file:line`, the runner's
own summary line and an output digest. Use it instead of piping a test run to
a file and reading it back with `tail -1` and `grep FAILED|ERROR`.

The run is a background durable job with a hard deadline. The model chooses a
runner and, optionally, a selector. The host owns the command line.

| Tool | Permission grade | What it does |
|---|---|---|
| `test_run` | execution: `plan` denies it, `manual` and `acceptEdits` ask, `auto` allows it | Plans the command, starts the job, waits up to `wait_seconds`. Returns the report, or the job status if the run is still going. |
| `test_run_result` | safe | Waits a bounded time for a run you started and returns its report or status. |
| `output_digest` | safe | Summarises a test-run job's output or a guarded log file. Owned by the diagnostics lane; see `docs/build-diagnostics-digest.md`. |
| `tool_inventory` | safe | Lists the tools installed on this host. Owned by the inventory lane; see `docs/host-tool-inventory.md`. |

All four tools are in the typed tool registry (`sonder_runtime/bootstrap/typed_tools.py`)
and in the native MCP catalog (`sonder_runtime/bootstrap/native_mcp.py`). They
route through the typed gateway, so every call is checked in this order:

1. schema
2. resource policy
3. permission modes
4. redaction
5. durable receipt

The legacy `server.py` `test_run` tool (the legacy agent's and the direct
MCP spelling) keeps its own arguments and result shape. Its pytest runs go
through this runner; see [the legacy `test_run`](#the-legacy-test_run).

## What the model may choose

| Argument | Type and bounds |
|---|---|
| `project` | A path. Must resolve inside the authorized file roots. |
| `runner` | `auto` (the default) or one of `pytest`, `unittest`, `ctest`, `cargo`, `go`, `dotnet`, `npm`, `pnpm`, `yarn`, `gradle`, `maven`, `make`. |
| `selector` | At most 200 characters. Must match the runner's grammar (see [Selector grammars](#selector-grammars)). |
| `timeout_seconds` | Clamped to 10 through the runner's maximum (1800). |
| `workers` | 1 to 8, capped at the CPU count. Accepted only by pytest (and only when pytest-xdist is installed for the chosen interpreter), ctest and go. |
| `wait_seconds` | 0 to 120. `test_run_result` accepts 0 to 60. |

There is no argument for argv, flags, environment variables or executable
paths. A selector can never start with `-`.

## Runners and templates

The planner runs `project_detect` on the project root (at most 4 levels deep,
at most 200 files) and maps each test command the project declares to a
host-owned template. The detected argv is never executed.

`auto` picks the shallowest directory. When two runners tie on depth, it
picks the one listed first in the table below. Every candidate is listed in
the plan.

| Runner | Command (`<python>` is the chosen interpreter) | Report | Default timeout |
|---|---|---|---|
| pytest | `<python> -m pytest -q -rfE --color=no -p no:cacheprovider -o junit_family=xunit2 --junitxml={report}` | JUnit XML | 600 s |
| unittest | `<python> -m unittest -v` | parsed from the unittest text output | 600 s |
| ctest | `ctest --test-dir {build_dir} --output-on-failure --no-tests=error --output-junit {report}` | JUnit XML with CMake 3.21 or later; the output digest otherwise | 600 s |
| cargo | `cargo test --color never --no-fail-fast` | parsed from the libtest text output | 900 s |
| go | `go test -json <package, default ./...>` | parsed from the JSON event stream in the output | 600 s |
| dotnet | `dotnet test <project> --nologo --logger trx;LogFileName=results.trx --results-directory {report_dir}` | TRX | 900 s |
| npm and pnpm | `npm test --` or `pnpm test --`, with reporter switches when the `test` script starts with `jest` or `vitest` | Jest JSON, Vitest JUnit, or the output digest | 600 s |
| yarn | `yarn test`, with the same reporter switches as npm | as npm | 600 s |
| gradle | `gradle test --console=plain --no-daemon` | `build/test-results/test/*.xml` | 1200 s |
| maven | `mvn -B test -Dstyle.color=never` | `target/surefire-reports/TEST-*.xml` | 1200 s |
| make | `make test` | the output digest | 600 s |

How the runners are prepared:

- **ctest** needs an existing configured build tree containing
  `CTestTestfile.cmake`. The planner looks for `build/`, `build-*/` or
  `out/build/*/`. If none exists it refuses with `CTEST_BUILD_TREE_MISSING`.
  Configure and build the project first; `test_run` never builds it for you.
- **pytest and unittest** use a project virtualenv when one exists: `.venv`,
  `venv` or `env` with a `pyvenv.cfg`, and a `bin/python` or
  `Scripts/python.exe` that resolves to a regular file. Otherwise they use the
  host inventory's `python3` or `python`. The plan note and
  `interpreter_source` say which was used.
- **gradle and maven** use `./gradlew` or `./mvnw` only when all of these hold:
  - the host has no `gradle` or `mvn`;
  - the host is POSIX;
  - the wrapper is a regular, executable, non-symlinked file inside the project.

  A plan note discloses the wrapper. Windows `gradlew.bat` and `mvnw.cmd` are
  refused.
- **Other executables** come from the host tool inventory. The inventory
  path is launched as recorded, not resolved, because multiplexing launchers
  such as rustup proxies dispatch on the name they run as.
- **tox, meson, swift and rake** are detected by `project_detect` but not
  supported here.

The code for all of this is in:

- `sonder_runtime/domain/testing/runners.py` (templates)
- `sonder_runtime/domain/testing/selectors.py` (grammars)
- `sonder_runtime/adapters/testing/detection.py` (planning)

## Selector grammars

| Runner | Selector | Becomes |
|---|---|---|
| pytest | node id `path[::Class]::test[param]`: project-relative, no `..`, no backslash | the node id |
| pytest | `k:<expr>`: identifiers joined by `and`, `or` and `not` | `-k <expr>` |
| unittest | dotted name | the dotted name |
| ctest | `[A-Za-z0-9_.:-]{1,128}`, optionally ending in `*` | `-R ^<escaped>$`, or `-R ^<escaped>` for the `*` form |
| cargo | `[A-Za-z0-9_:]{1,200}` | the filter |
| go | `./pkg/...` or `./pkg`, no `..` | the package |
| go | `run:TestName[/sub]` | `-run ^TestName$/^sub$` |
| dotnet | dotted identifier | `--filter FullyQualifiedName~<name>` |
| npm, pnpm and yarn | project-relative path, no `..` | the path |
| gradle | `[A-Za-z_*][A-Za-z0-9_.*]*` | `--tests <pattern>` |
| maven | `[A-Za-z_*][A-Za-z0-9_.*#+]*` | `-Dtest=<pattern> -Dsurefire.failIfNoSpecifiedTests=false` |
| make | none | selector refused with `SELECTOR_UNSUPPORTED` |

Rules that apply to every runner:

- No control characters.
- No whitespace, except inside a `k:` expression.
- No leading `-`.
- No shell syntax.

A path selector must also resolve, both lexically and by realpath, to an
existing path inside the project. Otherwise it is refused with
`SELECTOR_ESCAPES_PROJECT`.

## The report

`TestReport` is defined in `sonder_runtime/domain/testing/report.py`:

| Field | Meaning |
|---|---|
| `runner` | The runner that ran. |
| `status` | `passed`, `failed`, `error`, `no_tests`, `cancelled` or `timed_out`. |
| `job_id` | The durable job's id. |
| `command_digest` | Identifies the command. The per-run report path is replaced by its placeholder, so the digest is stable across runs. |
| `display_command` | The command with paths redacted. |
| `project` | Redacted label of the directory the run used. |
| `exit_code`, `duration_seconds` | As observed. |
| `totals` | `passed`, `failed`, `skipped`, `errors` and `total`. |
| `totals_source` | The report format the totals came from, or `summary_line`. |
| `totals_reliable` | False when the structured totals disagree with the runner's summary line, or when only the output digest was available. |
| `failures` | At most 50 entries of `{id, file, line, kind, message_excerpt}`. The excerpt is one line of at most 400 characters. |
| `summary_line` | The `tail -1` style line. |
| `digest` | The diagnostics lane's output digest. |
| `failures_truncated`, `report_truncated`, `output_truncated` | Set when the corresponding content was cut. |

The model-visible payload is at most 48,000 bytes: `fit_wire` drops the digest
tail first, then failures, then notes. The rendered text is at most 6,000
characters.

The first time a finished run is collected, its report is cached as
`report.json` with mode 0600 in the run's report directory.

A report is evidence written by project code, the test runner and the
project's own tests. It is not an attestation. Any test can print a
convincing summary line; `totals_reliable` only tells you whether two sources
agree.

### Report parser limits

The parsers are in `sonder_runtime/domain/testing/report_parsers.py`.

XML reports (JUnit and TRX):

- An XML document containing `<!DOCTYPE` or `<!ENTITY` is refused before the
  parser sees it.
- Each document is at most 8 MiB.
- At most 20,000 test cases are read.

Gradle and Maven report trees:

- Only files modified since the run started are read.
- At most 512 files and 16 MiB in total.
- The tree must stay inside the run directory, with no symlinks.

## Job lifecycle

1. **Plan.** The command is resolved and redacted.
2. **Permission.** For `test_run`, the permission evaluator
   (`DeveloperToolPermissionEvaluator`) plans the request first. It adds
   `resolved_command` to the arguments the permission modes decide on:
   `{runner, display_argv, cwd_label, command_digest}`. An unattended
   one-shot `/approve` is therefore bound to the exact command. A different
   project, selector or runner produces a different digest and needs a new
   approval. A plan the host refuses (a bad selector, no runner, or a project
   outside the roots) is refused before anyone is asked.
3. **Launch.** The job goes through the runtime's `ProcessJobProvider`:
   - kind `tool.test_run`, job id `test-run-<32 hex>`;
   - `inherit_environment=False`;
   - the deadline from the plan, which is killed as a process tree (a POSIX
     process group, or `taskkill /T` on Windows);
   - at most 64 descendants (128 for cargo, gradle and maven);
   - 4 GiB of memory.

   The host executable is checked again at launch. The report directory
   `<state>/test-runs/<token>` is created exclusively with mode 0700. At most
   64 run directories are kept.
4. **Reap.** One reaper thread per run (from `platform.runtime_threads`)
   waits on the provider, so the job reaches its terminal state even if
   nobody polls it.
5. **Collect.** The report file(s) are read. The output window is at most
   2 MB: 64 KiB from the head plus the tail. It is redacted, then digested.

The environment starts from the scrubbed child environment, which drops
secret, control and `SONDER_*` names. These are then removed:

- `PYTEST_ADDOPTS`, `PYTEST_PLUGINS`
- `GIT_DIR`, `GIT_WORK_TREE`, `GIT_INDEX_FILE`
- `GIT_ASKPASS`, `SSH_ASKPASS`

These are pinned:

- `CI=1`, `NO_COLOR=1`, `FORCE_COLOR=0`, `CLICOLOR=0`, `TERM=dumb`
- `PYTHONDONTWRITEBYTECODE=1`, `PYTHONIOENCODING=utf-8`, `PYTHONUNBUFFERED=1`
- `CARGO_TERM_COLOR=never`
- `DOTNET_CLI_TELEMETRY_OPTOUT=1`, `DOTNET_NOLOGO=1`, `DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1`
- `GIT_TERMINAL_PROMPT=0`

On Windows, a `.bat` or `.cmd` launcher (for example `npm.cmd`) receives only
arguments matching `^[A-Za-z0-9_./:=,+@\\-]+$`. If a report path breaks that
rule, the reporter switches are dropped, the run falls back to the output
digest, and the plan note says `batch_argument_unsafe`.

Each caller may have at most 2 runs going at once; a third is refused with
`TEST_RUN_BUSY`.

A caller only ever sees its own jobs. `test_run_result`, and the
diagnostics lane's `output_digest` for jobs, require kind `tool.test_run` and
a matching principal. Anything else answers `JOB_NOT_FOUND`, which is
indistinguishable from a job that does not exist.

### Waiting, polling and cancelling

- `test_run` waits up to `wait_seconds`, and never past the caller's own
  deadline. It then returns the report or a status view with the job id.
- If the caller's operation is cancelled during that wait, the run is
  cancelled.
- `test_run_result` waits up to 60 seconds more.

The model cannot cancel a run; every run has a hard deadline. The operator
cancels with:

- `POST /v1/jobs/{id}/cancel` (admin)
- REPL `/test cancel <job>`

The existing job endpoints also read the run: `GET /v1/jobs/{id}`,
`/v1/jobs/{id}/result` and `/v1/jobs/{id}/stream`.

### Over HTTP

HTTP callers start and read runs through the same typed tools. The gating is
the same as for `/v1/build/*`:

- the caller needs developer or admin authority
- every route is one typed gateway call, made as the authenticated principal
  with `source="http"`
- the permission modes grade that call unattended, because nobody is at a
  console

| Route | Typed call | Answers |
|---|---|---|
| `POST /v1/tools/test-run` with body `{project, runner, selector, timeout_seconds, workers, wait_seconds}` | `test_run` | `202` with the status view (`job_id`) while the run is going, `200` with the report once it is done. |
| `GET /v1/tools/test-run/{id}?wait_seconds=` | `test_run_result` | `202` with the status view while running, `200` with the report once done. Another principal's run is `404 JOB_NOT_FOUND`. |
| `POST /v1/tools/output-digest` with body exactly one of `job_id` or `path`, plus optional `tail_lines` and `max_failure_lines` | `output_digest` | `200` with the digest. The job must be a run the same principal owns. A path must be inside the guarded digest surface. |

Refusals and failures:

- Under `plan`, `manual` and `acceptEdits`, `test_run` is refused with
  `403 PERMISSION_DENIED`. The response carries the decision with its
  `call_id`, and the remedies. `POST /v1/approvals/<call_id>` can approve that
  exact call once.
- A plan the host refuses keeps its own code. For example, a bad selector is
  `400 INVALID_SELECTOR`.
- Other typed failures map to HTTP statuses:

  | Code | Status |
  |---|---|
  | `JOB_NOT_FOUND` | 404 |
  | `PROJECT_OUTSIDE_ROOTS`, `SELECTOR_ESCAPES_PROJECT`, `DIGEST_SOURCE_REJECTED` | 403 |
  | `TEST_RUN_BUSY` | 429 |
  | `DEVELOPER_TOOLS_UNAVAILABLE`, `RUNNER_UNAVAILABLE` | 503 |

- A malformed request never reaches the gateway. Examples are an unknown body
  field (there is no argv field of any kind), `job_id` and `path` together, or
  a repeated query parameter. These are answered with
  `400 INVALID_TEST_REQUEST`.

There is no HTTP cancel route in this family. Cancel with
`POST /v1/jobs/{id}/cancel` as above. The facade is
`sonder_runtime/interfaces/http/facades/testing_tools.py`. It shares its one
gateway call with the build routes, through `facades/typed_gateway.py`.

## Error codes

Failures are JSON of the form `{"ok": false, "error_code": ..., "message": ...}`.
Nothing returns an `ERROR:` string.

| Code | Meaning |
|---|---|
| `INVALID_SELECTOR` | The selector does not match the runner's grammar. |
| `SELECTOR_UNSUPPORTED` | The runner takes no selector of that kind. |
| `SELECTOR_ESCAPES_PROJECT` | A path selector points outside the project. |
| `WORKERS_UNSUPPORTED` | The runner does not accept a worker count. |
| `NO_RUNNER_DETECTED` | The project declares no supported tests. |
| `RUNNER_UNAVAILABLE` | The runner is not installed on this host. |
| `CTEST_BUILD_TREE_MISSING` | No configured CMake build tree. |
| `PROJECT_OUTSIDE_ROOTS` | The project is outside the authorized roots or the caller's lane grant, or is reached through a symlink. |
| `TEST_RUN_BUSY` | The caller already has 2 runs going. |
| `JOB_NOT_FOUND` | No such job visible to this caller. |
| `DEVELOPER_TOOLS_UNAVAILABLE` | The runtime did not compose the developer tools. |

## Lanes

In agent lanes, `compose_lane_test_tools` gives `LaneTestExecutor` the
developer executor as its `files` fallback. Its `CatalogPermissionEvaluator`
is a `DeveloperToolPermissionEvaluator`, so `test_run` is graded on its
resolved command in lanes as well.

Lanes themselves still offer only their fixed file-tool allowlist plus
`run_tests` (`application/agents/interactive_lanes.py`). Adding `test_run` to
that allowlist is a separate decision. If it is added, a lane's workspace
grant (`context.workspace_roots`) confines the project.

The host-configured `run_tests` catalog in
[agent lane test targets](agent-lane-test-targets.md) is unchanged.

## Limitations

- **This is not an OS sandbox.** Tests run project code with the caller's
  privileges, inside a scrubbed environment and resource bounds. That is why
  `test_run` is graded as execution.
- **The permission check and the run plan the command separately.** A project
  that changes between the two can pick a different candidate runner. The
  approval still binds runner, selector and project, and both plans use the
  same host-owned templates.
- **Exit codes are durable only once observed.** The process job records the
  observed exit code in the durable job record for failed runs as well as
  passing ones, so a report built after a runtime restart still classifies
  pytest exit 5 as `no_tests` and exits 2-4 as `error`. A run that was
  cancelled, or whose exit the runtime never observed (the runtime stopped
  while it ran), has no exit code to keep.

## The legacy `test_run`

`server.test_run` (`harness_tools.test_run` underneath) keeps its arguments
(`root`, `framework`, `path`, `pattern`, `verbose`, `coverage`, `timeout`) and
its result keys (`ok`, `returncode`, `timed_out`, `elapsed_ms`, `stdout`,
`stderr`, `command`, `cwd`, `framework`). Its renderer and the grounded-outcome
evidence read those keys.

- **pytest runs go through this runner** when the runtime composes it. The
  call is mapped to a single selector: `pattern` becomes `k:<pattern>` and
  `path` becomes a project-relative node id. It then runs with the host-owned
  template, the scrubbed environment, the hard deadline and process-tree
  cleanup, as the local owner.
- **`stdout` is rebuilt from the report** in pytest's own `-q` shape: one
  `FAILED`/`ERROR` line per failure, with the runner's summary line last. A
  reader that takes the final line or greps `FAILED|ERROR` keeps working.
  The result also carries `job_id`, `status` and `command_digest`.
- **A refusal is a legacy error result.** Examples are a pattern that is not a
  `k:` expression and a project outside the roots. The result has `ok: false`
  and an `error_code`, and nothing runs.
- **It shares the local owner's run slots.** Every legacy run is started as
  the local owner principal (`owner`), whoever called it: the agent, a fleet
  worker, the REPL, or an account's chat `/test_run`. The durable job is
  recorded as the owner's. The owner may run at most two structured test runs
  at once, and this cap covers legacy runs and the owner's HTTP and REPL runs
  together. A legacy run that finds both slots taken does not fail. It waits
  for a slot, asking again every 2 seconds, for at most its own `timeout`
  (600 seconds if none is given). The wait ends early if the caller is
  cancelled. If no slot frees by then, the answer is a `TEST_RUN_BUSY` error
  result. Parallel agent runs therefore take turns, two at a time. While
  they hold both slots, the owner's own HTTP `POST /v1/tools/test-run` is
  answered `429 TEST_RUN_BUSY`.
- **`extra_args_json` is retired.** Anything other than empty or `"[]"` is
  refused with `EXTRA_ARGS_RETIRED` ("extra_args_json retired; use
  path/pattern") before any runner starts. This includes malformed JSON,
  which the old path silently ignored. The parameter stays in the signature
  only so that refusal is explicit, and the agent help no longer lists it.

Some calls keep the harness's own command line:

- runs of other frameworks
- coverage runs (this runner has no coverage template)
- calls that pass both `path` and `pattern` (this runner takes one selector)
- runtimes that do not compose the developer tools
- a project the agent's dispatch binds that lies outside the configured file
  roots. The harness honors the one project the host bound for that agent
  run; this runner confines to the file roots alone and would refuse it with
  `PROJECT_OUTSIDE_ROOTS`, so such a project keeps the harness run it always
  had. Outside an agent dispatch the harness refuses the same project too.

That command line is now built entirely by the host: fixed flags, a
confined `path`, and `pattern` passed only as the value of `-k`.

Two differences from the old command line:

- **The interpreter changes.** The structured path uses the project
  virtualenv, or the host inventory's `python3`, as every structured run does
  (see [Runners and templates](#runners-and-templates)). It does not use
  Sonder's own interpreter. A project that relied on Sonder's interpreter
  having pytest installed now needs its own virtualenv. Otherwise the run
  reports `error` with the interpreter's "No module named pytest" line.
  A run that ends as `error` with no test results is given `returncode: -1`,
  the legacy marker for "no verdict", so grounded outcomes file nothing for
  it. Its real exit code stays in `exit_code`.
- **`verbose` has no effect** on the structured path, because the report
  already lists every failure.
