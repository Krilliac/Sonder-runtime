# Tools & Languages

Sonder exposes a guarded tool surface to the model (and to MCP clients).
Everything is host-policed: workspace containment, permission rules,
bounded output, and activity evidence apply to every call.

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
