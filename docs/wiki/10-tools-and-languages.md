# Tools & Languages

Sonder exposes a guarded tool surface to the model (and to MCP clients).
Everything is host-policed: workspace containment, permission rules,
bounded output, and activity evidence apply to every call.

## MCP client contract

Both MCP surfaces (`python -m sonder_runtime mcp`, the default, and
`mcp --native`) follow the same call contract:

- **Failures are tool errors.** A refused or failed call returns a
  `CallToolResult` with `isError: true` and the reason as its text. On the
  default surface this covers every tool reply that starts with `ERROR:`
  (for example a path outside the allowed roots, web tools disabled by
  `SONDER_WEB_TOOLS`, an agent that ran out of steps) as well as permission
  gate refusals. Clients that treated `ERROR:` text as success must check
  `isError` instead; the text is unchanged.
- **Unknown arguments are refused.** An argument name the tool's
  `inputSchema` does not list is rejected (`isError: true`, naming the
  accepted arguments) before the permission gate and before anything runs.
  A misspelt option no longer silently falls back to its default.
- **Frames are bounded and always answered.** The default surface refuses a
  stdio frame over 12,449,536 bytes (room for the largest call a tool accepts, a
  4 MB `file_batch_write`, however the client escapes it)
  and answers every malformed frame (invalid JSON or UTF-8, a batch array,
  `"jsonrpc": "1.0"`, an `id` that is not a string or integer, an unpaired
  surrogate escape) with a JSON-RPC `-32700`/`-32600` error, echoing the
  request `id` when it can be read. The native surface bounds frames at
  256,000 bytes.
- **`initialize`** reports the server's own capabilities (a client that sends
  `capabilities: {}` still sees `tools`) and the runtime build version as
  `serverInfo.version`. MCP Tasks on the native surface still require the
  client to advertise `tasks`; until it does, `tasks` is not advertised back.
- The native tool list is generated in
  [`runtime-reference.md`](../architecture/generated/runtime-reference.md#native-mcp-tools).
- **`agent_lane` needs a configured runtime.** Registering the bare
  loopback `python server.py` as the MCP server composes an application
  graph with no typed configuration, so it has no configured workspace
  grants to scope a lane to. There `agent_lane` refuses every action with a
  typed `DependencyUnavailable` ("agent conversations require a configured
  runtime") before the permission gate. Use `python -m sonder_runtime mcp`
  for agent conversations.

## Code execution — `run_code` / `/run`

Runs a bounded snippet and returns `{ok, returncode, stdout, stderr,
language, cwd, timeout}`. Timeout-clamped, output-trimmed, and confined to
the workspace cwd. It is a convenience runner, **not** a security sandbox.

**Supported languages (15):**

| Family | Languages |
|---|---|
| Scripting | Python, JavaScript (Node), TypeScript (`node --experimental-strip-types`), Bash/sh/zsh, Ruby, Perl, PHP, Lua, R |
| Compiled | C++ (g++/clang++/MSVC), C#, Go (`go run`), Java (JDK 11 single-file), Rust (rustc compile+run) |
| Shell | PowerShell on Windows; Bash/sh/zsh on POSIX hosts |

Language is chosen by fence: ```` ```rust ```` / ```` ```go ```` /
```` ```bash ```` etc., with common aliases (`rs`, `golang`, `ts`, `sh`,
`py`, ...). A missing interpreter returns a clean, actionable runner-level
error rather than a stack trace. `/run [seconds]` executes the last code
block from the previous response.

Related: `run_project` (bounded multi-file temp project with optional
build), `parallel_run_code` (many snippets concurrently), `script_run` /
`workspace_run` (argv-only execution of a real script/program). `script_run`
first applies the operator's static artifact-risk policy to its exact file.

## Formal proofs — Lean 4 / Mathlib

`verifiers.lean_check` checks a complete Lean 4 source artifact and returns a
verdict from Lean rather than asking a model to grade its own proof. It rejects
`sorry`, `admit`, `sorryAx`, `axiom`, and `constant` declarations before
invoking the toolchain, caps source, diagnostics, and runtime, and reports a missing
toolchain as unavailable rather than as a false proof failure.

A raw call is a compiler check. For task-bound evidence, provide
`expected_declaration` and `expected_type`; Sonder compiles the expected type in
a trusted module after the submission's compiler process exits and compares
kernel types in a separate audit, so submitted macros cannot rewrite or inspect
the randomized check while compiling. `solver.solve_lean` requires both fields
and will not accept a compiling proof of an unrelated proposition. That same
trusted Lean audit traces the declaration's
transitive axiom dependencies and rejects any axiom introduced by the submitted
module, including one installed through metaprogramming.

Contracts that use task-local definitions may add a caller-owned
`trusted_prelude`. Sonder compiles it separately and makes its declarations
available to both the expected type and the submitted proof without exposing
the contract axiom to the submission. This field is a trust boundary and must
never contain model-generated source.

Set `SONDER_LEAN_EXE` to the Lean executable for core-only checks. For practical
mathematics, also set `SONDER_LAKE_EXE` and `SONDER_LEAN_PROJECT` to a pinned
Lake project with Mathlib; checks then run as `lake env lean` and can import the
project's exact dependencies. Sonder never downloads packages during a proof
check. The repository's `lean-toolchain` pins the supported Lean release; see
[Formal reasoning setup](../runbooks/formal-reasoning.md).
When no executable or project override is configured, the core checker applies
that repository pin in its isolated directory and fails closed on a version
mismatch.

## Host tool versions — `environment_status` / `toolchain_status`

Use `environment_status` first to discover the local shells, toolchains, and
specialist utilities actually available on this host. To obtain a grounded
version for a discovered supported tool, call
`toolchain_status(name="cargo")` (or `lean`, `lake`, `git`, `cmake`, `sccache`, and similar
listed tools). It invokes only the tool's fixed, non-interactive version
switch; it accepts no executable path, command text, or extra arguments.

This is intentionally different from `run_code`: `run_code` executes source
snippets, so a command such as `cargo --version` must not be passed to it.
`toolchain_status` is local-only and returns a bounded, redacted result. It is
not a general shell or command-execution feature.

## Host tool inventory

The host tool inventory is a categorized, cached registry of the developer tools
installed on this host. It covers 14 categories: compilers, build systems, test
runners, linters/formatters, debuggers/profilers, package managers, runtimes,
containers/VMs, version control, database clients, media and document tools,
cloud CLIs, editors/IDEs, and shells.

It searches beyond `PATH`:

- on Windows: vswhere, the Windows SDK, App Paths, `py -0p`, scoop, Chocolatey
  and winget
- on macOS: Homebrew, the Xcode command line tools, and app bundles
- on Linux: common install prefixes

Each tool's version comes from the fixed probe listed in the registry, or from
installer metadata. The snapshot is kept for 24 hours.

Once a snapshot exists, agents receive a one-line, path-free capability summary
in their environment brief. Administrators can read the inventory with
`GET /v1/tools/inventory` and force a rediscovery with
`POST /v1/tools/inventory/refresh`.

Discovery never executes:

- GUI launchers
- tools whose version query goes to the network
- project-local binaries
- Store aliases

See [Host tool inventory](../host-tool-inventory.md) for the sources, bounds,
redaction rules and limitations.

## Test runs — `test_run` / `test_run_result`

`test_run` runs a project's test suite with a host-owned command for the
detected runner (pytest, unittest, ctest, cargo, go, dotnet, npm/pnpm/yarn,
gradle, maven, make). The model picks only the runner and an optional
grammar-checked selector such as `test_mod.py::test_bad`, `k:fast` or
`run:TestX`. The run is a permission-gated durable background job with a hard
deadline.

It returns a typed report: totals, failures with `file:line`, the runner's
summary line and an output digest. A still-running job returns its id;
`test_run_result` waits for it. See
[Structured test runs](../structured-test-runs.md).

## Structured data — `data_inspect`

Read-only, never-executing structured preview of a data file inside
allowed roots. Understands by suffix:

| Type | Preview |
|---|---|
| JSON | type, keys/key-count or item count, pretty head |
| JSONL / NDJSON | record count, first-record keys |
| TOML | tables, table count |
| YAML | type, keys (PyYAML optional; raw head fallback) |
| CSV / TSV | columns, column count, row count, a sample row |
| SQLite (`.db`/`.sqlite`) | table list with row counts (read-only URI) |
| ZIP / TAR / TGZ | member list, count, expanded size |
| INI / CFG | sections, section count |
| unknown text | line count + head; binary → signature bytes |

Malformed content is reported as a finding, not a crash; oversize files
are refused by a byte budget. This fills the gap between raw `file_read`
and image `image_inspect` — the model can understand a database or a data
file's *structure* without dumping raw bytes.

`data_query` adds bounded read-only retrieval for SQLite, JSON, JSONL, CSV,
and TSV. SQLite accepts exactly one `SELECT` or CTE through a read-only URI,
with an authorizer plus row, column, byte, and time ceilings. Text formats use
only structured exact-equality filters and field/JSON-pointer projections; no
expressions or file content are executed.

`data_convert` deterministically converts JSON arrays, JSONL, CSV, and TSV
using an explicit ordered list of exact top-level fields. Preview mode fully
validates and sizes the conversion without touching disk. Apply mode writes a
same-directory staging file and atomically publishes it only if the destination
does not exist. UTF-8, headers, finite values, nesting, fields, rows, columns,
input bytes, and output bytes are all validated under hard ceilings; no
expressions, implicit type inference, or overwrite mode are available.

## Guarded filesystem tools

`file_find`, `file_read`, `file_read_range`, `file_write`, `file_batch_write`, `file_edit`, `text_patch`,
`file_copy`, `file_move`, `file_delete`, `directory_tree`, `directory_create`, `workspace_inventory`,
`workspace_compare`, `text_search`, `script_search`, `program_search`, `image_inspect`, `log_inspect`, `artifact_risk_inspect`, `data_convert`, `archive_create`, `archive_list`,
`archive_extract`, `repo_log`,
`repo_show`, `repo_blame`. All are
confined to `SONDER_FILE_ROOTS`, honor the permission policy
([Security Model](09-security-model.md)), and record byte/line accounting
into the activity trail. `file_delete` is dry-run unless an explicit
confirm string matches.

`repo_log`, `repo_show`, and `repo_blame` expose structured, read-only Git history from an
exact repository root. They use fixed argv-only Git commands, never discover a
parent repository, reject unsafe revision/path syntax, disable pagers and
external diff/text-conversion helpers, and enforce count, byte, and time caps.
`repo_show` additionally requires one contained non-sensitive regular file,
both in the worktree and at the requested revision, before it returns patch
content; an unfiltered commit can never expose unrelated files.

`file_copy` and `file_move` transfer exactly one regular file between explicit
source and destination paths. They are binary-safe, refuse overwrite by
default, reject symlink/junction and sensitive-control-state paths at both
ends, and cap each transfer at 64 MiB. Copy commits through a same-directory
temporary file; no-overwrite publication is atomic and never replaces a
competitor. Move stages the same bounded copy at the destination, revalidates
the source, and deletes it only after publication. They never recurse and never
invoke a shell or network service. Repository agents rebase and validate both
paths against their exact assigned project root; autopilot accepts these tools
only with overwrite disabled and no caller-supplied approval or extra root.

`file_batch_write` accepts a JSON list of explicit `create` or `overwrite`
operations. It prevalidates every target before writing, caps per-file and
aggregate bytes, rejects duplicate/sensitive/symlink targets, and makes a
best-effort rollback if any write fails.

`archive_list` prevalidates bounded ZIP/TAR manifests without extraction.
`archive_extract` uses the same fail-closed validation, streams members into a
sibling staging directory, and promotes only to a new non-overwriting project
destination. Traversal, absolute paths, links/devices, encrypted entries,
collisions, nested archives, sensitive paths, and archive bombs are rejected.

`archive_create` accepts explicit project-contained inputs and a new destination,
supports ZIP and TAR, and defaults to reproducible metadata. It performs a full
bounded preflight, refuses links and sensitive/control state, streams stable file
handles, revalidates input mutation, and publishes through a non-overwriting
sibling staging file.

`artifact_risk_inspect` performs non-executing static inspection of guarded
PDFs, Windows PE files, ELF and Mach-O executables, scripts, and opaque binary
artifacts. Results contain only format metadata and named indicator counts—not
embedded strings, URLs, memory addresses, or raw bytes. Scan/source/decode/time
ceilings are hard, and partial, encrypted, malformed, or unsupported analysis
is explicit. A high-risk result means the file contains suspicious static
evidence; it is not a proof of malware, and no-finding is not a guarantee of
safety.

For exact script execution, `SONDER_EXECUTION_RISK_POLICY` selects `off`,
`report` (default), `deny-high`, `deny-medium`, or `deny-unknown`. Per-call
`risk_policy` can make enforcement stricter but never weaker than the operator
setting. Current `deny-*` modes conservatively refuse every launch, including a
below-threshold file, because the runner cannot portably guarantee that an
interpreter opens the same file handle that was inspected. `report` is advisory.
Program execution without an exact inspectable file remains outside this static
gate and should be isolated separately.

`process_list` and `process_memory_risk_inspect` are host-observation tools for
defensive analysis on Windows. Both require the exact operator opt-in
`SONDER_PROCESS_INSPECTION=enabled:bounded-read-only`. Inventory exposes only
bounded PID, parent PID, executable-name, and thread-count metadata. Memory
inspection accepts one PID and returns only fixed risk-indicator names/counts
from private readable memory and aggregate scan accounting under hard byte,
region, and time ceilings. Read failures and partial reads make the result
explicitly incomplete rather than clean. It
does not return memory content, discovered strings, paths, addresses, or command
lines and never requests write/injection/debug rights. Unsupported platforms,
protected processes, access denial, and incomplete scans fail explicitly.

`text_patch` previews strict unified diffs rooted at an explicit project
directory. With `apply=true`, it performs an all-file transaction for create
and modify operations only. Context must match exactly; deletes, renames,
binary/non-UTF-8 data, sensitive paths, links, escapes, and over-budget input
are rejected.

`workspace_compare` compares two guarded files or directory trees without
returning their contents. It emits a deterministic relative-path inventory of
entry type, size, and SHA-256 plus exact added/removed/changed/same counts.
Entry, file, aggregate-byte, detail, output, and time ceilings are enforced;
sensitive/control paths, special files, and symlink or junction traversal are
rejected.

`log_inspect` reads one guarded UTF-8 text log through a validated no-follow
file handle. Fixed host parsers extract common text and JSON-log timestamps,
levels, and sources; the result summarizes error/warning clusters, repeated
messages, and bounded first/last-failure context. Prefix or tail inspection is
available under file, scan-byte, line, per-line, result, output, and time caps.
Callers cannot supply regular expressions or executable parsing rules.

## Build/test output digest — `output_digest` / `/digest`

`output_digest` summarizes one guarded log file, or the caller's own test-run
job output, into:
- the final line (`tail -1`)
- the recognized run summary, for pytest, unittest, cargo, go, ctest, jest,
  vitest, dotnet, maven, gradle and make
- the `FAILED`/`ERROR` lines
- the first compiler or test errors, parsed into typed diagnostics: gcc,
  clang, ld, MSVC, dotnet, rustc, tsc, eslint, go, Python tracebacks and
  pytest
- repeated-error groups
- a short tail

It is the structured form of `pytest ... > out.txt; tail -1 out.txt; grep -E
"^(FAILED|ERROR) " out.txt`. Everything is redacted before parsing and is
bounded.

- Files go through the same guarded no-follow window as `log_inspect`, and
  credential stores are refused.
- For a model, a job digest is limited to that principal's own `test_run` and
  lane-test jobs.

The legacy `test_run`, `build_run`, `lint_run` and `typecheck_run` renderings
end with a bounded `digest:` block.

In the REPL:
- `/digest <job|path>` digests a job or a log.
- `/tools` shows the categorized host tool inventory.
- `/test [runner] [selector]` starts a structured test run.

`/tools` no longer aliases `/activity`.

The details are in [Build diagnostics and output digest](../build-diagnostics-digest.md).

## C/C++ builds — `build_model` / `build_job` / `build_fix`

These three tools let an agent inspect, build and repair a C/C++ project.

- **`build_model`** describes the build without running anything. It reads the
  CMake File API reply, `compile_commands.json` or `.sln`/`.vcxproj`, and
  returns targets, configs, platforms, toolchains, compile units, PCH and
  presets as labels.
- **`build_job`** runs `configure`, `build`, `compile_one` or `include_trace`
  as a permission-gated background job. `build_job_result` waits for the
  typed, attributed report.
- **`build_fix`** repairs a failing target. It runs a bounded loop that edits
  only project sources: never build scripts, generated files or the sources of
  build-time tools. Each attempt is verified with `compile_one`, then with a
  target build. `build_fix_restore` writes the stored originals back.

The host builds every command from closed templates. The model only names
things the parsed model contains. Utility and custom targets such as `deploy`
are refused unless the operator allows them.

Under the default `manual` mode, a build from the console is asked once. The
same build from HTTP or native MCP is refused and the refusal names the
remedies. Approving a `build_fix` lets that fix's own in-scope writes proceed
without further prompts, until the fix job ends.

The REPL facade provides `/build`, `/fix-build` and `/fix-build-restore`. See
[C++ build model, build jobs and the build-fix loop](../architecture/CPP-BUILD-FIX.md)
and [Build tools security](../security/BUILD-TOOLS.md).

## Crash and profile digests — `/crash` / `/profile`

Crash captures and profiler output from native builds become two small typed
results, a crash report and a profile digest. Raw debugger output and capture
bytes never reach the model; strings copied from a capture are labelled
untrusted.

- Pure readers (no process launched): Windows/Breakpad/Crashpad minidumps,
  ELF cores, sanitizer logs, valgrind XML, macOS `.ips`; callgrind, Chrome
  trace JSON, Tracy/WPA/PIX/Superluminal CSV, `heaptrack_print` and
  `perf report` text.
- Host engines, as permission-gated jobs from host-owned argv templates: cdb,
  gdb, lldb, eu-stack, rust `minidump-stackwalk`, `llvm-symbolizer`, `perf`,
  `heaptrack_print`, `tracy-csvexport`, xperf.
- Symbol-server downloads happen only from the attended console after
  consent and a y/N on the exact command; model, MCP and HTTP requests for
  them are refused.

In the REPL:
- `/crash <dump|core|log>` digests a crash; `/crash triage <dir>` buckets a
  folder of dumps by signature.
- `/crash fix last` prints fatal diagnostics in compiler form, a local source
  excerpt and a repro test.
- `/profile <capture>` summarizes hot paths and frame spikes.

Admin HTTP routes live under `/v1/tools/crash-*`, `/v1/tools/profile-*` and
`/v1/tools/debug-runs/<id>`.

The details are in [Crash and profile digests](../crash-profile-digest.md).

## Other tool families

- **Local service probe:** `local_service_probe` performs bounded,
  unauthenticated `GET`/`HEAD` checks against explicit-port HTTP/HTTPS URLs.
  Every DNS answer must be loopback (`127/8` or `::1`) and is rechecked before
  a direct numeric-address connection. The probe ignores proxy environment,
  sends no cookies or authorization, rejects credential-bearing URLs and
  non-loopback redirects, and caps timeout, headers, body, and preview output.
  It is intentionally direct-MCP-only: agents, repository sessions, loops, and
  autopilot cannot invoke it because localhost responses may contain secrets.
- **Web (opt-in):** `web_search`, `web_fetch`, `weather_lookup`,
  `approximate_location_lookup` — gated by `SONDER_WEB_TOOLS`.
- **Artifacts:** `artifact_generate` / `artifact_verify` /
  `ground_artifact` — stdlib images, SVG, Office, audio, GLB, etc., with
  deterministic verification.
- **Tasks/checklists:** `task_*`, `checklist_*` — shared todo/checklist
  state across console, app, agents, and MCP.
- **Memory/learning:** see [Memory & Learning](06-memory-and-learning.md).
- **Ops/health:** `status`, `diagnostics`, `context_health`,
  `activity_status`, `self_heal_check`/`_repair`.

## Read-only tool set & speculation

The read-only subset (inventory, tree, find, read, search, `data_inspect`, `log_inspect`,
image inspect, memory search, status) is what the speculation engine may
run speculatively while the model thinks — never a mutating or executing
tool ([Speculation & Prediction](11-speculation-and-prediction.md)).

`command_registry_list` enumerates the full command surface by category,
name, or risk.
