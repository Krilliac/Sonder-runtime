# C++ build model, build jobs and the build-fix loop

This is the contract of the C/C++ build tools: `build_model`, `build_job`,
`build_job_result`, `build_fix`, `build_fix_result` and `build_fix_restore`.
The requirements it touches are in the
[master specification](SONDER-MASTER-IMPLEMENTATION-SPEC.md). The security
model, with the reason for each bound, is in
[Build tools security](../security/BUILD-TOOLS.md).

## What the tools do

- `build_model` describes a project's build without running anything. It reads
  a CMake File API reply (any client's codemodel), then `compile_commands.json`,
  then `.sln`/`.vcxproj`, then checks whether `build.ninja` or a `Makefile`
  exists. It returns the build system, generator, configs, platforms, targets,
  toolchains, compile units, PCH and presets. Every value is a label. No host
  path is returned.
- `build_job` runs `configure`, `build`, `compile_one` or `include_trace` as a
  durable, permission-gated process job (`JOB_KIND` `tool.build_job`). The host
  renders the argv from a closed template set. The model can only name members
  of the parsed model: target, config, platform, preset, file and generator.
  `build_job_result` waits (bounded) for the report, or cancels the caller's own
  job with `cancel=true`.
- `build_fix` runs a bounded repair loop as a durable job (`tool.build_fix`):
  - it compiles the focus file;
  - it proposes a patch limited to editable project sources;
  - it checks the patch with `compile_one`, then with a target build;
  - it keeps the best candidate and reverts regressions.

  `build_fix_result` reports the outcome, or cancels the fix and its child
  build. `build_fix_restore` writes the stored originals back.
- Every report carries `world`, `network` and `isolation_truth`. The last one
  uses the EXEC-006 labels: `unverified` for host builds, or
  `failure_isolation_only` for process-tree containment. Network enforcement is
  reported on its own and is never described as a security boundary.

The build-graph statement (F4): a build runs the project's own custom
commands, and those may execute tools built from the same tree. So `build_fix`
never edits:

- the sources of build-time tool targets;
- generated sources;
- files listed as CMake inputs.

It stops with `BUILD_TIME_TOOL_SOURCE` instead. It never launches the binaries
it produces.

## Behavior status

| Behavior | Status | Boundary |
| --- | --- | --- |
| Typed descriptors, gateway routing, permission grades | Implemented | Six closed schemas; none shares a legacy name. The legacy `build_run` is untouched (F2). |
| Plan-bound approvals and plan-mode refusal | Implemented | Approvals bind to `resolved_command`. A refused plan never prompts. `plan` mode refuses before planning. |
| Build-fix grant | Implemented | Covers only in-scope typed writes during the job. See Grant semantics. |
| Network decision | Implemented | `allow_network=true` needs a separate `build_network` decision. |
| HTTP routes `/v1/build/*` | Implemented | Developer authority is required. The call runs through the typed gateway as the caller, unattended. |
| REPL `/build`, `/fix-build`, `/fix-build-restore` | Experimental | The facade module and command specs exist. The REPL lane wires `repl.py` and `command_catalog.py`. |
| Model-context build line | Experimental | Keyed by principal. It is emitted only when a surface declares the turn's principal. |
| clangd navigation (lane D) | Experimental | Composed only when the inventory has clangd. Tested on Linux with clangd 18. |
| MSBuild and vcvars execution | Degraded | Linux can parse these models but not run them (`RUNNER_UNAVAILABLE`). Windows execution is validated only by fakes. |

## Composition

`bootstrap/build_tools.py` owns the composition. `bootstrap/app.py` calls it next
to the developer tools, in both places:

- the main typed facade: `build_tool_executor(build_tools, developer_tool_executor(...))`;
- the lane-tests `files=` executor.

The generalized `DeveloperToolPermissionEvaluator` receives
`resolvers=build_permission_resolvers(...)` and
`grant_authorities=(build_grants,)`. `lane_tests.CatalogPermissionEvaluator`
takes the same keywords and keeps resolving `test_run` without them.

Composition is lazy. Nothing is probed, read or launched when it runs. Each of
the following leaves the services `None`:

- a missing build package (lanes A/B1);
- a missing fix-loop package (lane B2), which leaves only `fix` as `None`;
- a failing adapter constructor.

Every build tool then answers `BUILD_TOOLS_UNAVAILABLE` and every other tool is
unaffected.

The evaluator resolves requests from a map (F12):

- `ToolResolver(resolve, on_surface, after_allow)` per tool name;
- `test_run` is one entry;
- `build_job` and `build_fix` are added by the build tools.

Grant authorities are consulted first, and only for requests that carry an
`approval_token`.

## Permission resolution

| Mode | `build_model` / results | `build_job` / `build_fix` | `build_fix_restore` |
| --- | --- | --- | --- |
| `plan` | allowed | refused before planning | refused |
| `manual` at the console | allowed | the REPL asks once, then forwards with `gate=surface` | asked |
| `manual` unattended (HTTP, MCP, worker) | allowed | refused, with a `call_id` and the standard remedies | refused |
| `acceptEdits` | allowed | asked, so unattended calls are refused | allowed |
| `auto` | allowed | allowed | allowed |

The standard remedies are:

- an allow rule scoped to the tool;
- `acceptEdits`/`auto`;
- the console;
- a one-shot `permission_approve` of the exact `call_id`.

A one-shot approval covers the planned call. The planned call includes the
template id, command digest, target, config, platform, world and network. A
different target, config or platform digests differently and needs its own
approval.

## Grant semantics

The grant (F1) exists because a background loop has nobody to ask. Without
it, every write the fix makes is refused unattended under `manual`.

1. The evaluator allows `build_fix` in one of these ways:
   - the console prompt;
   - an allow rule;
   - `auto`;
   - a one-shot approval of the planned call.

   It then mints a grant from the fix plan's `grant_spec` and `scope`. The
   grant lives in process memory only. It is keyed to the principal and the
   request.
2. The executor claims the grant for the same request and principal, passes
   the token to `BuildFixService.start(request, context, grant_token=...)`,
   and binds it to the returned job id. An unclaimed grant dies after 300
   seconds.
3. The fix loop's typed writes (`text_patch`, `write_file`, `read_file`) carry
   the token as `approval_token`. The grant admits a write only when all of
   these hold:
   - the principal matches;
   - the job is still active;
   - the path is absolute, reached without links, inside the project root and
     outside the build directory;
   - `EditScope.allows(rel)` holds;
   - no guard knob (`extra_roots`, `bypass`, `developer_authorized`) is set;
   - `write_file` uses `mode=overwrite` on an existing file;
   - the patch neither creates, deletes nor renames a file;
   - the budgets hold: at most 6 files and 400 changed lines.

   The receipt's `policy_match` then names `build_fix_grant:<plan_digest>`. If
   any condition fails, normal grading applies, which refuses the write
   unattended.
4. The grant ends when the job ends, at `expires_at`, or on revoke. It:
   - never adds roots;
   - never lifts `plan` mode;
   - never covers the network unless the fix itself was approved with
     `allow_network`;
   - covers child builds only for the plan's template family and its
     (build dir, target, config, platform, world) tuple
     (`covers_child_build`).

## Surfaces

- Typed tools: native MCP and every typed-gateway caller
  (`bootstrap/native_mcp.py` `_BUILD_TOOLS`).
- HTTP (`interfaces/http/facades/build_tools.py`, routed by `serve.py`):
  - `GET /v1/build/model`;
  - `POST /v1/build/jobs` (202 while running, 200 for a report);
  - `GET /v1/build/jobs/{id}`;
  - `POST /v1/build/jobs/{id}/cancel`;
  - `POST /v1/build/fix` (202);
  - `GET /v1/build/fix/{id}`;
  - `POST /v1/build/fix/{id}/cancel`;
  - `POST /v1/build/fix/{id}/restore`.

  POST bodies are JSON objects; send `{}` when the route takes no fields.
- REPL (`interfaces/repl/facades/build_tools.py`): `BUILD_COMMAND_SPECS` and
  `register_build_commands(register, facade_getter=...)`. `BuildReplFacade`
  runs every command through `execute_tool(tool_name, arguments)`.
- Brief: `install_build_brief(services, inventory)` chains a build line of at
  most 220 characters onto the capability summary, whose total cap is 480. A
  surface declares the turn's principal with
  `build_brief_principal(principal_id, project_label=...)`. Without one there
  is no build line (F22).

### Hook instructions for the REPL and server lanes

- `repl.py`: call `register_build_commands(register, facade_getter=...)`, where
  `register(name, handler, spec)` installs `handler(arg) -> str`. The facade
  getter returns `BuildReplFacade(execute_tool)`. `execute_tool` routes through
  `application.tools` with `source="repl"`, and uses `gate="surface"` after the
  console has asked.
- `command_catalog.py`: add `BUILD_COMMAND_SPECS` as catalog entries. Each
  entry's `tools` names the typed tools that grade its risk.
- `server.py` (server lane):
  - wrap the agent prompt assembly in
    `build_brief_principal(principal, project_label=...)`;
  - add `grounded_outcomes.VERIFIERS` and `verification_reach` entries for
    `build_job` and `build_fix`;
  - point the legacy `build_run` help text at `build_job`.

## Configuration

Parsed by `platform/config.build_tools_config_from_env`. A malformed value is a
configuration error. It never widens anything.

| Key | Meaning | Default |
| --- | --- | --- |
| `SONDER_BUILD_PROFILES` | Operator profile file. On POSIX it must be a regular file with mode 0600, owned by the runtime user. It is ignored on Windows until ACL verification exists. | unset |
| `SONDER_BUILD_ENV_PASSTHROUGH` | Extra variable names a job inherits. The adapter denylist still applies. | none |
| `SONDER_BUILD_NETWORK` | `enforce`, `default` or `advisory` | `default` |
| `SONDER_BUILD_MAX_TIMEOUT_SECONDS` | Operator deadline cap (30..86400) | 7200 |
| `SONDER_BUILD_FIX_WORLD` | `host` or `container` | `host` |
| `SONDER_BUILD_UTILITY_TARGETS` | Utility and custom targets the operator allows | none |
| `SONDER_BUILD_USER_PRESETS` | Read `CMakeUserPresets.json` | on |
| `SONDER_BUILD_CLANGD_CONFIG` | Let clangd read a project `.clangd` | off |
| `SONDER_BUILD_FIX_MODEL_ROUTE` | Model route the fix loop proposes with | `codegen` (local) |
| `SONDER_BUILD_FIX_PROPOSE_ONLY_OK` | Allow `build_fix apply=false` | off |

## Requirement map

| Requirement | Where |
| --- | --- |
| TOOL-001 one gateway, one name per schema | the typed descriptors, the typed-gateway routing on every surface, and distinct names from the legacy `build_run` |
| TOOL-002/003 resource-aware policy | the `build:*` policy rules and the execution/mutation/safe grades |
| TOOL-004 approval bound to the resolved command | `build_permission_resolvers` (`resolved_command`, `build:plan-refused`) |
| TOOL-007 receipts | the gateway receipts and the durable audit, with grant writes named by `policy_match` |
| JOB-001..005 | the build and fix jobs (lanes B1/B2) and cancellation through the result tools |
| LOOP-006/007/008 | the fix loop (lane B2); grant writes go through the typed writes and their effect-journal intents |
| EXEC-001/002/006 | host world per run, the IsolationTruth labels on reports, and build-time tool sources excluded from edits |
| REPO-004/007 | clangd `BuildNavigator` and `ClangdSymbolTransport` (lane D) |

## Platform matrix

- **Linux:** CMake with Ninja, Ninja Multi-Config or Unix Makefiles; the File
  API; the compile database; `compile_one`; `include_trace`; `unshare -rn`
  network enforcement; clangd. MSBuild models are parsed, and running them
  answers `RUNNER_UNAVAILABLE`.
- **Windows:**
  - vswhere/vcvars capture for Ninja/NMake with cl, clang-cl and
    `include_trace`;
  - the VS generators;
  - msbuild with per-project attribution;
  - the network is advisory.

  Operator profiles are ignored until ACL checks exist.
- **macOS:** CMake with Ninja or Makefiles, AppleClang and clangd. Xcode trees
  are modelled and answer `ACTION_UNSUPPORTED`.

## Evidence

Recorded on the Linux validation host (Ubuntu 24.04, root, cmake 3.28, ninja
1.11, gcc 13, clang 18, clangd 18.1.3 installed with `apt-get install -y
clangd`):

- Unit and surface suites:
  - `tests/test_build_tool_descriptors.py`
  - `tests/test_build_permissions.py`
  - `tests/test_build_fix_grant_evaluator.py`
  - `tests/test_build_executor.py`
  - `tests/test_build_http_facade.py`
  - `tests/test_build_repl_facade.py`
  - `tests/test_build_brief.py`
  - `tests/test_build_tools_composition.py`

  The grant suite drives real `text_patch` and `write_file` writes through the
  typed gateway under `manual` mode, as an unattended worker.
- `tests/test_build_clangd_transport.py` runs against the real clangd:
  - definition and hover;
  - diagnostics that match g++;
  - the `.clangd` opt-in;
  - an idle shutdown that kills the process group;
  - LspTransport conformance;
  - no `.cache` written into the project.
- `tests/test_build_live_smoke.py`, for g++ and clang++, on a checkout merged
  with the lane A and B1 packages:
  1. `build_model` answers `BUILD_TREE_MISSING`.
  2. `configure` succeeds, with `network=enforced_off` and
     `isolation_truth=unverified`.
  3. The `deploy` utility target is refused at planning.
  4. The build fails, attributed to `math.cpp`.
  5. The in-scope repair succeeds.
  6. The rebuild succeeds.

  The `build_fix` step of that test runs once the lane B2 package is present.

Caveat: the host runs as uid 0, so file-mode checks of private directories
prove nothing here. The unprivileged checks belong to lane B1
(`setpriv --reuid=65534`). No Windows, MSVC or Docker daemon was available.
