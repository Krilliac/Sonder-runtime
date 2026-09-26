# Build diagnostics and output digest

Sonder has **one** diagnostics parser for compiler, linker, type-checker,
linter, interpreter and test-runner output. It lives in
`sonder_runtime/domain/diagnostics/`. On top of it sits the **output digest**,
a bounded summary of any job output, log file or text. The digest is the
structured form of this shell habit:

```sh
pytest ... > out.txt; tail -1 out.txt; grep -E "^(FAILED|ERROR) " out.txt | cut -c1-230
```

Related pages:
- Structured test runs, `docs/structured-test-runs.md` (`/test`, `test_run`)
- Host tool inventory, `docs/host-tool-inventory.md` (`/tools`, `tool_inventory`)
- [Tools & Languages](wiki/10-tools-and-languages.md)
- [Natural-language tools](NATURAL_LANGUAGE_TOOLS.md)

## Layers

| Layer | Module | Role |
|---|---|---|
| domain | `domain/diagnostics/model.py` | `Diagnostic`, `DiagnosticGroup`, `DiagnosticSet`, `Severity`, `DiagnosticTool`, signature templates |
| domain | `domain/diagnostics/parsers.py` | `parse_line`, `parse_diagnostics`, `error_lines` (the codegen counter) |
| domain | `domain/diagnostics/summary.py` | `RunSummary`, `parse_pytest_summary`, `find_summary` |
| domain | `domain/diagnostics/digest.py` | `OutputDigest`, `digest_text`, `render_digest` |
| application | `application/diagnostics/ports.py` | `TextWindow`, `TextWindowSource`, `JobOutputReader` |
| application | `application/diagnostics/service.py` | `OutputDigestService` (`digest_file`, `digest_job`, `digest_text`) |
| adapters | `adapters/diagnostics/sources.py` | `GuardedFileWindowSource`, `RegistryJobOutputReader` |
| bootstrap | `bootstrap/diagnostics.py` | `compose_output_digest_service`, `job_output_reader` |
| interfaces | `interfaces/repl/facades/developer_tools.py` | `/tools`, `/test`, `/digest` rendering |

The domain modules are pure. They use only the standard library, never open
files or run processes, and evaluate nothing.

## Parser grammars

Each typed `Diagnostic` has these fields: `tool`, `severity` (`fatal`, `error`,
`warning` or `note`), `file` (`/`-normalized), `line`, `col`, `code` and
`message`.

| Tool | Recognized shape | Code |
|---|---|---|
| `gnu` (gcc, clang, ld) | `file:line[:col]: (fatal error\|error\|warning\|note): msg [-Wflag]` | `-Wflag` |
| `gnu` (ld) | `file:(.text+0x9): undefined reference to ...`, `/usr/bin/ld: ...`, `collect2: error: ...` | none |
| `msvc` | `file(line[,col]): error C2065: msg`; a trailing `[x.vcxproj]` is stripped | `C2065` |
| `msvc_link` | `main.obj : error LNK2019: msg` | `LNK2019` |
| `dotnet` | csc `CS0103`, `MSB`, `NETSDK` and `NU` codes, with or without `(line,col)` | as printed |
| `rustc` | `error[E0308]: msg`; the location comes from the `-->` line within 4 lines | `E0308` |
| `tsc` | `file(l,c): error TS2322: msg` and the pretty `file:l:c - error TS2322: msg` | `TS2322` |
| `eslint` | the stylish block (a path header, then `  l:c  error  msg  rule`) and the unix `file:l:c: msg [Error/rule]` | rule name |
| `go` | `./x.go:l:c: msg` (go build and go vet); `# pkg` headers are ignored | none |
| `python` | a `Traceback` block, where the **last** frame gives the file and line; bare `SyntaxError` blocks too | exception type |
| `pytest` | `FAILED node::id - msg`, `ERROR node - msg` | `FAILED` / `ERROR` |
| `generic` | a location-less `error: msg` that is not a rustc diagnostic | none |

Some rustc lines carry no information and are dropped:
- `warning: ... generated N warnings`
- `error: aborting due to ...`

The rustc line `could not compile` is kept, with an empty file.

A `code` is at most 64 characters, and a message is one line of at most 400
characters.

## Dedupe and grouping

Diagnostics are deduplicated on the exact tuple `(tool, severity, file, line,
col, code, message)`, keeping the original order.

Each diagnostic has a `signature()`: the first 16 hex characters of sha256
over `tool|severity|code|template`. The template replaces quoted names,
paths, hex values and numbers with placeholders. So `'alpha' undeclared` and
`'beta' undeclared` fall into **one** group.

Groups are sorted by severity, then by count (highest first), then by first
appearance. Each group records up to 8 files.

`counts` gives the per-severity totals over every distinct finding, taken
before the list is capped. A capped list therefore never under-reports.

## Run summaries

`find_summary` scans the last 200 lines. It tries each runner's grammar in
priority order, and each one scans from the end of the output:

| Runner | Example line |
|---|---|
| pytest | `= 3 failed, 10 passed, 2 skipped, 1 error in 4.21s =`, and its `-q` form; `no tests ran` |
| unittest | `Ran 4 tests in 0.01s` followed by `OK (skipped=1)` or `FAILED (failures=1, errors=1)` |
| cargo | `test result: FAILED. 1 passed; 1 failed; 1 ignored; ...`, summed across crates |
| go | `ok  pkg 0.01s`, `FAIL pkg`, `FAIL`; these count packages, so the test counts stay empty |
| ctest | `50% tests passed, 1 tests failed out of 2` |
| jest | `Tests:       1 failed, 4 passed, 5 total` |
| vitest | `Tests  1 failed \| 4 passed (5)` |
| dotnet | `Failed!  - Failed: 1, Passed: 4, Skipped: 0, Total: 5, Duration: 20 ms` |
| maven | `Tests run: 5, Failures: 1, Errors: 0, Skipped: 0`; per-class lines with `Time elapsed` are skipped |
| gradle | `5 tests completed, 1 failed` |
| make | `make: *** [Makefile:3: test] Error 2`, which gives a status of failed with no counts |

Because of the priority order, a pytest summary is preferred over the
`make: *** ... Error 1` line that a Makefile wrapper prints after it.

## Digest fields

`digest_text(text, ...)` returns an `OutputDigest` with these fields:

| Field | Meaning | Cap |
|---|---|---|
| `final_line` | the last non-empty line (`tail -1`) | 400 chars |
| `summary` | the `RunSummary` from `find_summary`, or none | none |
| `failure_lines` | lines matching `^(FAILED\|ERROR)\b`, `\bFAIL(ED)?\b`, `^error[:\[]`, `: error`, `Error N$` or `---- x stdout ----`, deduplicated (the `grep` step) | at most 200 lines, 300 chars each |
| `first_errors` | the first diagnostics, unique by signature, with fatals and errors first | at most 50 |
| `groups` | signature groups | at most 50 |
| `counts` | severity totals, plus `failure_lines` and `error_lines` | none |
| `tail` | the last lines | at most 200 lines, 300 chars each |
| `truncated` / `scan_truncated` | set whenever any cap or window dropped content | none |

The input is capped at 50,000 lines, keeping a head and a longer tail, and at
4,096 characters per line.

`to_wire()` stays under 48,000 UTF-8 bytes. When the digest would not fit, it
drops the tail first, then groups, failure lines and first errors, and marks
`wire_truncated`.

`render_digest(d, max_chars=4000)` is hard-capped at 16,000 characters. It
starts with `summary: <line>`, or with `final: <line>` when there is no
summary.

### Worked example

Here is a real pytest run, from `-q -rfE --continue-on-collection-errors`:

```text
..FsF                                                                    [100%]
... tracebacks ...
=========================== short test summary info ============================
FAILED test_mod.py::test_bad - assert 1 == 2
FAILED test_mod.py::test_other_bad - ValueError: boom
ERROR test_broken.py
2 failed, 2 passed, 1 skipped, 1 error in 0.03s
```

Its digest renders as:

```text
summary: 2 failed, 2 passed, 1 skipped, 1 error in 0.03s
result: pytest (status=failed passed=2 failed=2 skipped=1 errors=1 total=6 duration=0.03s)
counts: error=3 failure_lines=3 error_lines=3
scanned: 33 lines, 1460 bytes
failure lines:
  FAILED test_mod.py::test_bad - assert 1 == 2
  FAILED test_mod.py::test_other_bad - ValueError: boom
  ERROR test_broken.py
first errors:
  test_mod.py error [FAILED] assert 1 == 2
  test_mod.py error [FAILED] ValueError: boom
  test_broken.py error [ERROR] test_broken.py
tail:
  ... (the last 20 lines)
```

`tests/test_output_digest.py` checks this output against the shell idiom:
- `final_line` equals `tail -1`
- `failure_lines` equals the `grep` output

## Sources and guards

Every source applies the runtime Redactor to the raw text **before** parsing,
so no field can carry an unredacted value. If redaction fails, the redactor
returns the sentinel `[REDACTION_FAILED]`, and that sentinel is digested as-is.

### File source

- The file source uses only the guarded no-follow window from `log_inspect`,
  `read_guarded_text_window`:
  - allowed roots and sensitive-directory rejection
  - symlink and reparse-point rejection
  - an identity re-check after the file is opened
  - a file limit of 64 MB, a scan window of 4 MB (tail mode) and a 5 s limit
- It also refuses these before reading any bytes, and again on the path of the
  opened file:
  - credential stores (`credential_read_component`, `.env*`)
  - `SECRET_FILES` and `SECRET_SUFFIXES`
- A refusal raises `DigestSourceRejected`, which is `Forbidden` and
  `PermissionError`, with code `DIGEST_SOURCE_REJECTED`.

### Job source

- `RegistryJobOutputReader` pages the durable registry's watermark stream, up
  to 400 pages, 8 MiB scanned and 5 s.
- It keeps the 64 KiB head plus a rolling tail, at most 2 MB in total.
- It marks `truncated` when:
  - the middle was dropped
  - the registry's bounded retention had already dropped output
  - an event was spilled
- The registry retains a bounded window per job, so for large outputs the
  digest is of the retained tail. That tail is where the summary line is.

### Ownership

- A model or typed caller can digest only jobs of kind `tool.test_run` or
  `agent_lane.test` that belong to the same principal. Any other job answers
  `NotFound`, which cannot be told apart from a missing job.
- The local REPL operator (`operator=True`) can digest any job.

## Surfaces

| Surface | What it does |
|---|---|
| REPL `/digest <job_id\|path>` | An argument shaped like a job id that names an existing job gives a job digest, as operator. Otherwise the argument is treated as a guarded path. |
| REPL `/tools [refresh [full]\|<category>\|<name>]` | The categorized host inventory. The operator sees full paths. |
| REPL `/test [runner] [selector]`, `/test status\|result\|cancel <job>` | Starts a structured test run and waits for its report, printing an elapsed line every 10 s. Ctrl+C cancels the job. |
| Legacy MCP `output_digest(path, job_id, tail_lines)` | Returns compact JSON `{"ok": ...}`. Pass exactly one of `path` or `job_id`. Read-only and local-only. |
| Legacy MCP `tool_inventory(category, name, refresh)` | Returns compact JSON `{"ok": ...}`. Paths are redacted. Read-only and local-only. |
| Legacy `test_run`, `build_run`, `lint_run`, `typecheck_run` | Their rendering now ends with a bounded `digest:` block, at most 2000 characters. `format_run_result(digest=False)` is unchanged byte for byte. |

A runtime that did not compose the developer tools answers as follows:
- the REPL prints `developer tools are not composed in this runtime`
- the legacy tools return `{"ok": false, "error_code": "DEVELOPER_TOOLS_UNAVAILABLE"}`

`/tools` **no longer aliases `/activity`**. Use `/activity` for recent tool
activity.

The console permission gate grades the new commands by these stand-ins:

| Command | Stand-in | Grade |
|---|---|---|
| `/test` | `test_run` | execution: it asks in `manual` and is refused in `plan` |
| `/tools` | `tool_inventory` (same inventory service; `/tools` shows the unredacted view) | safe |
| `/digest` | `log_inspect` | safe |

Plain-language routes:

| Phrase | Command |
|---|---|
| "show installed tools", "tool inventory" | `/tools` |
| "run the tests" | `/test` |
| "run cargo test" | `/test cargo` |
| "summarize the output of job X" | `/digest X` |

"which toolchains are installed" still routes to `/env`.

## Codegen unification

`codegen_loop.count_errors` and `codegen_loop.DEFAULT_ERROR_RE` delegate to
`domain/diagnostics/parsers.error_lines` and `DEFAULT_ERROR_LINE_PATTERN`.
The behaviour is byte for byte the same, which
`tests/test_codegen_diagnostics_unification.py` pins. As a result, the
codegen loop's masking and scoring guards, and the `server.py` codegen build
loop, read the same distinct error lines as before.

## Known limitations

- A digest is evidence produced by the project's own output. It is not an
  attestation that the tests ran or passed.
- The parsers recognize common shapes. A tool that prints something else still
  gets its final line, failure lines and tail, but no typed diagnostics.
- A project-bound agent's `output_digest(path=...)` stays inside the project
  root. It still reads only paths within the configured allowed roots, and a
  project outside them fails closed with `DIGEST_SOURCE_REJECTED`.
