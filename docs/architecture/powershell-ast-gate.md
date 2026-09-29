# PowerShell AST permission gate

Worktree baseline: `45a3e093aa423c3a23f9e356419972d2332015e5`.

The argument-aware permission decision raises an uninspectable PowerShell call
to `dangerous`. It never changes the catalog's tool-level grade, lowers an
unclassified grade, or changes rule/mode precedence. Plan denies; attended
manual/acceptEdits/auto ask; unattended callers are refused unless the existing
explicit allow rule or exact one-shot approval authorizes the call. An explicit
deny still wins. The reason names PowerShell inspection without echoing source.

## Inventory

The inventory used tracked-file searches for `powershell`, `pwsh`,
`EncodedCommand`, `Invoke-Expression`, shell/process launchers, and permission
decider calls, followed by tracing each executable input to its runner.

| Location | Execution or classification responsibility |
| --- | --- |
| `permission_modes.py` (`risk_of`, `decide`, `_decide`) | Catalog grade and mode/rule/approval decision. The new per-call AST floor is applied here; `risk_of` stays unchanged. |
| `permission_rules.py`, `domain/execution/policy.py` | Persistent tool-name glob rules; no shell parsing. |
| `adapters/security/permission_policy.py`, `permission_evaluator.py` | Shared policy provider and typed-gateway adapter. Already forward request arguments. |
| `reloadable_mcp.py` | Legacy MCP entry gate forwards the exact arguments. |
| `server.py` agent/control/loop gates | Agent and catalogued control calls forward arguments. Loop previously discarded them; it now forwards the action only when it launches PowerShell (`powershell_gate.loop_gate_arguments`), with execution alias precedence preserved. Control `/run` binds its selected history block only when that block is PowerShell. |
| `interfaces/http/serve.py` | Catalogued calls forward arguments. `/run` and window aliases bind the exact selected block at the existing gate when it is PowerShell (`slash_run_arguments`); other languages keep the argument-free decision. Legacy natural PowerShell run intents use this gate too; other languages retain their existing path. |
| `interfaces/repl/repl.py` | Console catalogued calls previously dropped kwargs; they now forward them only for calls that launch PowerShell (`powershell_arguments`, a structural check that never starts the parser). `/run` aliases bind the last code block to the one existing gate only when it is PowerShell. |
| `server.run_code`, `adapters/execution_tools/code_runner.py` | Inline code becomes a temporary `.ps1`, run through `pwsh` or `powershell`, `-NoProfile -File`. Both inline and separate-window console forms are covered. Root `code_runner.py` is a compatibility alias. |
| `server.run_project`, packaged `code_runner.run_project` | Explicit command lists can launch PowerShell. Inspection uses the submitted in-memory project files, including command cwd, without writing them. Auto-detection has no PowerShell runner. |
| `server.workspace_run`, `adapters/filesystem/workbench.run_program` | `shell=False` argv execution; independently rejects inline PowerShell switches. The permission gate also inspects explicit PowerShell argv and available script/stdin source. |
| `server.script_run`, `workbench.run_script`, `adapters/tool_executor.py` | Native `run_script` and legacy `script_run` choose the interpreter by suffix. `.ps1` source is read using the existing workspace resolver. Artifact-risk policy remains a separate check. |
| `adapters/artifact_risk.py` | Existing byte-pattern risk heuristics include encoded PowerShell and download/execute patterns. Enforcing artifact policies require an exact sealed handoff; pathname launches under report/off retain their existing race limitations. |
| `server.isolated_run`, `adapters/execution/isolated_runner.py` | Container argv, never a host shell string. Explicit PowerShell argv is inspected; unavailable container script files require approval. Root `isolated_runner.py` is an alias. |
| `server.parallel_run_code`, `grounding.run_code_jobs`, `_run_powershell` | Language-specific code and appended check are inspected together. The existing grounding runner contains a preexisting execution-policy override; this change does not use it for parsing or validation. |
| `parallel_generate_run_languages`, `campaign_generate_compile_execute_record` | Initial decisions match `origin/main`, including default languages. After generation, `powershell_gate.run_generated_code` inspects PowerShell plus appended checks immediately before entering the execution runner. Opaque candidates are skipped with `requires approval: <reason>`. |
| `server.build_run`, `harness_tools.build_run` | Windows custom command uses `cmd /d /s /c`. Explicit PowerShell text is inspected; cmd expansion around it is opaque. Auto-detected build commands are unchanged. |
| `server._agent_project_guard_error` | Existing project command/interpreter guard; remains independent and is not weakened. |
| `bootstrap_engine.py`, `sonder_headless.py` | Fixed Windows helper/launch scripts, not model-supplied command inputs. |
| `adapters/artifact_fetch.py`, `adapters/ollama_lifecycle.py`, `platform/system_profile.py` | Fixed signature, process and system probes use PowerShell internally. They are not reclassified by this argument-aware tool gate. |
| `platform/environment_probe.py`, `domain/host_tools/registry.py` | Executable discovery and PowerShell version probe metadata. |
| `orchestrator.py`, `endless_train.py`, `domain/campaign_prompt.py`, `domain/prompt_templates.py`, `domain/natural_model_request.py`, `domain/computer_use/intent.py`, `preference_learning.py`, `adapters/mcp_tool_manifest.py`, `adapters/observability/repl_machine_output.py` | Language selection, prompt/default text, help, or rendering references; no additional PowerShell interpreter boundary. |

Internal Python calls to runner functions retain the existing trusted-caller
contract. The gate applies at tool surfaces, not to fixed host diagnostics or
every operating-system process created indirectly by Python, git, or another
literal executable. Literal external programs remain subject to existing tool
permissions and filesystem/artifact guards; this is not an OS sandbox.

The two multi-language generators inspect each generated candidate, including
every campaign repair, at the execution boundary. Their worker threads have no
interactive approval channel, so a non-inspectable script returns a typed
approval skip through the existing per-candidate result path. It never reaches
the language runner. The report shows `[SKIP]` and `requires approval: <reason>`;
other languages continue. Campaign skips stop repairs and do not record a
passing/failed outcome or distill a pitfall. A program printing the same text
cannot impersonate that typed skip. Approval of the generator does not approve
unseen source; an operator can submit the concrete script through the existing
source-aware `run_code` approval path.

## Parser and failure behavior

`adapters/security/powershell_ast.py` starts an absolute installed PowerShell
binary with a fixed helper, no profile, no interactive input, hidden window and
an eight-second timeout. Candidate source is JSON stdin data, never interpolated
into the helper. The helper calls the native
[`Parser.ParseInput`](https://learn.microsoft.com/en-us/dotnet/api/system.management.automation.language.parser.parseinput)
API and recursively visits nested ASTs; it never compiles or invokes them.
The native parser handles PowerShell escapes, comments, quoting, scriptblocks,
subexpressions and parse errors that a regex or POSIX shell parser cannot model.

Encoded executable switches, expression invocation, dynamic executable names,
scriptblock construction/invocation, opaque PowerShell host arguments, dynamic
Start-Process targets, Add-Type, command rebinding through aliases/function
providers, unseen script files and parse errors require
approval. Literal nested command strings are parsed recursively. Dynamic data
arguments and ordinary static methods are allowed when they do not conceal an
executable operand. Start-Process argument strings are interpreted in the
context of the selected PowerShell executable.

Unavailable parsers, timeout, nonzero exits, invalid protocol responses and
input limits fail closed. Positive syntax verdicts have a bounded cache tied
to the parser's path/stat identity and exact source. Files are reread, not cached
by path. No parser process is launched for unrelated tools or argumentless
catalog decisions. The helper is not a substitute for verifying the contents
of external executables or preventing pathname replacement races.

## Reproducing the preservation measurement

Run `D:/sonder-eco/venv-rt/Scripts/python.exe -B scripts/measure_powershell_ast.py --measure`.
The JSONL corpus retains source provenance and distinguishes command candidates
from prose/templates. It includes complete tracked `.ps1` sources, PowerShell
Markdown fences, inline examples, Python literals/argv and structured strings,
plus common benign controls. Dynamic templates cannot be resolved to runtime
values by a static collector; their source fragments are retained as candidates.
The measurement parses candidates only and never executes them.

To avoid thousands of process startups, measurement parses batches of at most
16 sources with the identical native helper, then supplies each exact source's
verdict to the real permission decider. Batches have a ten-second timeout and
fall back to individual inspection if needed. Runtime availability, timeout,
protocol handling and nonexecution are separately tested through the actual
single-source helper. The persisted summary records the helper's SHA-256.

The default `docs/architecture/powershell-ast-corpus.jsonl` contains individual
results and is explicitly gitignored as a reproducible local artifact. Do not
commit it. `--measure` also writes the small, trackable sibling
`powershell-ast-corpus.summary.json` for machine-readable totals. `--out` can
select a different local corpus path, with its summary written alongside it.
The collector's
`command`/`candidate` label is heuristic: harvested strings include prose,
metadata, partial commands and unresolved templates, not just complete calls.

## Measured qualification (2026-09-29)

| Measurement | Before / after |
| --- | --- |
| Catalog entries | 339 name-only risk grades unchanged (224 distinct tool names). |
| Argumentless decisions, four modes and both attendance states | 1,792 / 1,792 unchanged; zero parser calls. |
| Default `{}` calls in attended auto | 339 / 339 unchanged; generators defer AST inspection until their PowerShell source exists. |
| Harvested strings from 4,549 tracked files | 1,372 inspected; 810 unchanged, 562 raised to dangerous/ask. |
| Fully inspectable strings | 810 / 810 unchanged. |
| Complete `.ps1` sources | Six inspected; six raised for dynamic invocation, dynamic process targets or splatting. |
| Complete PowerShell Markdown fences | 45 inspected; 33 unchanged, 12 raised. |
| Common benign controls | All six unchanged: Get-ChildItem, Test-Path, git status, git diff, python version and pytest collection. |
| Native corpus batches | 86; zero timeouts, zero fallbacks, no harvested command execution. |

Of the 562 changed harvested strings, 397 have parse errors (many are fragments
or templates), 109 start PowerShell without visible command/stdin, 20 reference
unseen script files, and 36 have other opaque constructs. These are input-string
counts, not a claim that 562 production tool calls changed.

Fresh parser startup measured 1.126 seconds on this host. Repeated positive
cache checks averaged 0.056 ms; unrelated-tool selection averaged 0.182 us and
launched no helper. Earlier concurrent cold starts hit the five-second limit;
the final runtime bound is eight seconds, and actual timeouts still fail closed.

## Review follow-up validation (2026-09-29)

`tests/test_powershell_generated_gate.py` adds 143 cases: 128 initial decisions
compared to the actual `origin/main:permission_modes.py` across both tools,
all four modes, both attendance states and allow/ask/deny/no-rule policies;
four default-argument execution/prompt-count cases; four native-parser generator
cases; appended-check coverage; four parser-failure cases; a campaign repair
case; and a forged approval-output case. All passed.

| Default arguments in auto mode | origin/main prompts | Current prompts | Generated PowerShell outcome |
| --- | --- | --- | --- |
| `parallel_generate_run_languages`, inspectable | 0 | 0 | 1 executed, all 5 candidates proceed |
| `campaign_generate_compile_execute_record`, inspectable | 0 | 0 | 5 executed, all 24 candidates proceed |
| `parallel_generate_run_languages`, opaque | 0 | 0 | 1 skipped with `requires approval: <reason>`; other 4 proceed |
| `campaign_generate_compile_execute_record`, opaque | 0 | 0 | 5 skipped with that reason; other 19 proceed, no repair or grading of skips |

The four default-argument cases use deterministic generated responses and runner
spies; native-parser tests separately exercise both generators with real AST
inspection and mocked execution. No model calls or candidate processes run in
these tests. Prompt counts reflect the real initial permission decision and
assert that workers never call a second permission prompt or console input.
Manual/acceptEdits/plan behavior and explicit rules retain origin/main decisions.

The combined PowerShell AST, permission, generated-code and surface tests plus
`test_repl_input.py` produced **362 passed, 14 setup errors**. Every PowerShell
gate case passed. The 14 errors were REPL `tmp_path` fixtures, blocked before the
test body by WinError 5 on `.psast-check/pytest-of-Nathan`. Neighboring campaign
and generation regression tests produced **15 passed, 1 setup error**: an
unrelated game-control-command case needs `unattended_effects_allowed` from the
excluded root conftest. These are not full-suite qualification.

Runs used the requested `D:/sonder-eco/venv-rt/Scripts/python.exe`,
`--noconftest -p no:cacheprovider`, and worktree-local state/temporary paths.
The normal pytest harness remains unavailable under the temporary-directory ACL
restriction. Native verification used Windows PowerShell; PowerShell 7 and Unix
were not separately exercised. The real generator default comparison was first
run against the unfixed diff: all four scenarios failed with **0 versus 1**
initial approval prompts, demonstrating the reported regression.

The requested lint ratchet and architecture checks both exited 0. `server.py`
is 26,934 lines (below the existing 26,942 limit); the HTTP module is 8,360 lines.
`git diff --check` passed. No public tool schemas, SDK projections or committed
catalogs changed. No push, PR or deployment.

## Argument-binding narrowing (2026-09-29)

Surfaces that decided without arguments on `origin/main` (console catalogue,
loop actions, the `/run` aliases on console, app and control) now bind call
arguments **only** when the call launches PowerShell. Every other call keeps
origin/main's argument-free decision, call digest and one-shot approval
behaviour. `tests/test_powershell_gate_surfaces.py` pins this for Python
`run_code`, `workspace_run` of a non-PowerShell program and `file_write` on
each surface; those ten cases fail when the narrowing is removed.
