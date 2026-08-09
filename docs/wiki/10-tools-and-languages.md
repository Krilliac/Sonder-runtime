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
| Shell | PowerShell |

Language is chosen by fence: ```` ```rust ```` / ```` ```go ```` /
```` ```bash ```` etc., with common aliases (`rs`, `golang`, `ts`, `sh`,
`py`, ...). A missing interpreter returns a clean, actionable runner-level
error rather than a stack trace. `/run [seconds]` executes the last code
block from the previous response.

Related: `run_project` (bounded multi-file temp project with optional
build), `parallel_run_code` (many snippets concurrently), `script_run` /
`workspace_run` (argv-only execution of a real script/program).

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

## Guarded filesystem tools

`file_find`, `file_read`, `file_read_range`, `file_write`, `file_batch_write`, `file_edit`,
`file_delete`, `directory_tree`, `directory_create`, `workspace_inventory`,
`text_search`, `script_search`, `program_search`, `image_inspect`, `archive_create`. All are
confined to `SONDER_FILE_ROOTS`, honor the permission policy
([Security Model](09-security-model.md)), and record byte/line accounting
into the activity trail. `file_delete` is dry-run unless an explicit
confirm string matches.

`file_batch_write` accepts a JSON list of explicit `create` or `overwrite`
operations. It prevalidates every target before writing, caps per-file and
aggregate bytes, rejects duplicate/sensitive/symlink targets, and makes a
best-effort rollback if any write fails.

`archive_create` accepts explicit project-contained inputs and a new destination,
supports ZIP and TAR, and defaults to reproducible metadata. It performs a full
bounded preflight, refuses links and sensitive/control state, streams stable file
handles, revalidates input mutation, and publishes through a non-overwriting
sibling staging file.

## Other tool families

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

The read-only subset (inventory, tree, find, read, search, `data_inspect`,
image inspect, memory search, status) is what the speculation engine may
run speculatively while the model thinks — never a mutating or executing
tool ([Speculation & Prediction](11-speculation-and-prediction.md)).

`command_registry_list` enumerates the full command surface by category,
name, or risk.
